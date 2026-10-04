"""审核 AI 的分层指标评测入口（工具层 / 任务层 / 解释层 / 成本层 + 回归门禁）。

用法::

    # 默认离线跑（不需要任何 API key），并对比 baseline
    python -m scripts.evaluate_ai_review

    # 只跑评测，不做回归比对
    python -m scripts.evaluate_ai_review --no-baseline

    # 跑完把当前指标写成新基线
    python -m scripts.evaluate_ai_review --save-baseline

    # 额外跑 judge 校准，输出与人工标注的一致率
    python -m scripts.evaluate_ai_review --calibrate

    # 真实模型参与（Agent 决策 + LLM judge），会出网
    python -m scripts.evaluate_ai_review --live

    # 顺带写 Allure 结果，复用平台已有的报告栈
    python -m scripts.evaluate_ai_review --allure

四层指标定义见 ``ai_service/eval/metrics.py``；golden 集的来源与
「审核结论层为什么算不了」见 ``ai_service/eval/golden.py`` 的模块 docstring。

退出码：
    0 —— 无回归
    1 —— 有指标退化超过容忍度
    2 —— 评测本身没跑起来（样本为空、参数不对）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _make_stdout_utf8() -> None:
    """让报告里的 ✗ / ⚠ 在 GBK 控制台也不炸。

    Windows 默认码页是 GBK，打印这些符号会抛 UnicodeEncodeError —— 而且恰好
    只在**有退化**时才抛（通过时那句用的是纯中文），于是 ``--save-baseline``
    在真正需要重设基线时反而走不到。把流本身换成 UTF-8 比逐个符号降级更稳。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 取决于运行环境
            pass


_make_stdout_utf8()

from ai_service.agent import AgentBudget  # noqa: E402
from ai_service.eval.golden import DEFAULT_LABELS_PATH, DEFAULT_TARGET_SIZE, load_golden_set  # noqa: E402
from ai_service.eval.metrics import METRIC_DIRECTIONS  # noqa: E402
from ai_service.eval.report import (  # noqa: E402
    DEFAULT_BASELINE_PATH,
    DEFAULT_CALIBRATION_PATH,
    EvaluationReport,
    evaluate,
    run_calibration,
    save_baseline,
)
from ai_service.llm import build_llm_client  # noqa: E402


LAYER_TITLES = {
    "tools": "工具层",
    "task": "任务层",
    "explain": "解释层",
    "cost": "成本层",
}

METRIC_LABELS = {
    "sequence_accuracy": "工具序列完全匹配率",
    "sequence_subsequence_accuracy": "工具序列子序匹配率",
    "tool_set_accuracy": "工具集合覆盖（顺序无关）",
    "parameter_accuracy": "工具参数正确率",
    "avg_steps": "平均步数（越低越好）",
    "reason_code_match_rate": "原因码命中率（代理指标）",
    "evidence_rate": "有引用依据的比例",
    "action_rate": "有处置建议的比例",
    "escalation_accuracy": "转人工判定准确率",
    "verdict_accuracy": "结论正确率（人工标注）",
    "degraded_rate": "降级运行比例（越低越好）",
    "truncated_rate": "被预算截断比例（越低越好）",
    # 双判（P2.3）。改判率刻意不带「越高/越低越好」—— 它是描述性指标，
    # 不是优化目标，所以也不进回归门禁
    "llm_override_rate": "AI 改判率（描述性，非优化目标）",
    "llm_failure_rate": "复核失败率（越低越好）",
    "harmful_override_rate": "错误放行率（越低越好）",
    "llm_adjudication_accuracy": "复核正确率（对人工标注）",
    "relevance": "相关性",
    "accuracy": "准确性",
    "completeness": "完整性",
    "usefulness": "有用性",
    "avg_tokens": "平均 token 用量（越低越好）",
    "avg_provider_tokens": "其中 provider 回传的真实用量",
    "provider_usage_rate": "用量的真实来源占比",
}

#: 成本层在离线跑时恒为 0（不调模型），需要一句解释，否则读者会以为指标坏了
COST_LAYER_NOTE = (
    "此处统计审核 Agent 的 token；离线为 0。"
    "平台双判可能另外请求 AI 服务，其用量不在此统计，0 不能证明所有链路都未调用模型。"
)


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="评测审核 AI 的四层指标并做 baseline 回归门禁。"
    )
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_PATH, help="标注文件路径")
    parser.add_argument(
        "--target-size", type=int, default=DEFAULT_TARGET_SIZE, help="golden 集目标条数（上限 50）"
    )
    parser.add_argument(
        "--baseline", type=Path, default=DEFAULT_BASELINE_PATH, help="baseline 文件路径"
    )
    parser.add_argument("--no-baseline", action="store_true", help="不做回归比对")
    parser.add_argument("--save-baseline", action="store_true", help="把当前指标写成新基线")
    parser.add_argument(
        "--tolerance", type=float, default=0.05, help="允许的相对退化比例，默认 0.05"
    )
    parser.add_argument("--calibrate", action="store_true", help="额外跑 judge 校准")
    parser.add_argument(
        "--calibration-path",
        type=Path,
        default=DEFAULT_CALIBRATION_PATH,
        help="校准样本路径",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="真实调用模型（Agent 决策 + LLM judge）。默认离线，只加这个开关才出网",
    )
    parser.add_argument("--max-steps", type=int, default=6, help="Agent 步数预算")
    parser.add_argument(
        "--ocr-snapshot",
        type=Path,
        default=None,
        help="OCR 快照路径（默认 data/annotations/ocr_outputs.json）。"
             "字段输入优先取自快照，这是 OCR/双判面可信的前提",
    )
    parser.add_argument(
        "--no-ocr-snapshot",
        action="store_true",
        help="强制用 labels.json 标注真值当字段输入（旧口径，仅用于对照）",
    )
    parser.add_argument("--json", dest="json_path", type=Path, help="把完整报告写成 JSON")
    parser.add_argument("--allure", action="store_true", help="写 Allure 结果到 reports/allure-results/")
    parser.add_argument(
        "--allure-dir", type=Path, default=ROOT_DIR / "reports" / "allure-results"
    )
    return parser.parse_args(argv)


# ── 输出 ──────────────────────────────────────────────────────────────────────

def print_report(report: EvaluationReport) -> None:
    golden = report.golden
    print("=" * 72)
    print("审核 AI 分层指标评测（工具 / 任务 / 解释 / 成本）")
    print("=" * 72)
    print(f"golden 集：{golden['size']} 条（来源 {golden['labels_path']}）")
    print(f"judge 引擎：{report.judge_engine}")
    print(f"分布：{json.dumps(golden['balance'], ensure_ascii=False)}")
    print()

    for layer, title in LAYER_TITLES.items():
        block = report.metrics.get(layer) or {}
        if not block:
            continue
        print(f"── {title} ──")
        for name, value in block.items():
            label = METRIC_LABELS.get(name, name)
            print(f"   {label:<24} {value:.4f}")
        if layer == "cost":
            print(f"   · {COST_LAYER_NOTE}")
        print()

    print("── 说明 ──")
    for note in golden.get("notes") or []:
        print(f"   · {note}")
    if golden.get("verdict_layer_available") is False:
        # 上面已经逐条列了「为什么不可用」。这里只补一句**下一步做什么** ——
        # 重复一遍「缺标注」不如告诉人去跑哪个脚本。
        print(
            "   · 决策层（真实审核结论正确率）本轮不计入："
            "填好标注后重跑本命令即可启用。生成待填清单："
            "python -m scripts.make_verdict_worksheet"
        )
    print()


def print_regression(report: EvaluationReport) -> None:
    print("── 回归门禁 ──")
    if report.baseline_path is None:
        print("   未启用基线比对")
    elif report.passed:
        print(f"   指标基线回归通过（基线 {report.baseline_path}）；不代表真实 AI 可用")
    else:
        print(f"   发现 {len(report.alerts)} 项退化（基线 {report.baseline_path}）：")
        for alert in report.alerts:
            print(f"     ✗ {alert.describe()}")
    if report.missing:
        print(f"   ⚠ 基线里有但本轮未算出的指标：{', '.join(report.missing)}")
    if report.unregistered:
        print(
            f"   ⚠ 未登记方向的指标（不参与门禁，属静默漏检）："
            f"{', '.join(report.unregistered)}"
        )
    print()
    print("── 真实 AI 验证（独立于回归门禁）──")
    execution = report.execution
    print(f"   执行模式：{execution.get('mode', 'unknown')}")
    print(f"   模型决策样本：{execution.get('model_decision_samples', 0)}；"
          f"降级样本：{execution.get('fallback_samples', 0)}")
    print(f"   真实模型响应证据：{'有' if execution.get('real_model_verified') else '未验证'}")
    print(f"   真实服务全程验证：{'通过' if execution.get('live_validation_passed') else '未通过/未执行'}")
    print("   模型质量：未作独立人工验收；确定性 rubric 高分不代表模型高分。")
    print()


def print_calibration(report: EvaluationReport) -> None:
    if report.calibration is None:
        return
    print("── Judge 校准 ──")
    print(f"   {report.calibration.describe()}")
    for note in report.calibration.notes:
        print(f"   · {note}")
    print()


def write_allure(report: EvaluationReport, directory: Path) -> int:
    """按 Allure2 的结果格式落盘，复用平台已有的报告栈而不是另起一套。

    一个用例一个 ``*-result.json``，附一个 ``*-container.json`` 挂载它们。
    ``allure serve reports/allure-results`` 即可看到这份评测。
    """
    import uuid

    directory.mkdir(parents=True, exist_ok=True)
    parent = uuid.uuid4().hex

    def write(name: str, payload: Dict[str, Any]) -> None:
        (directory / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    children: List[str] = []
    for row in report.rows:
        result_id = uuid.uuid4().hex
        children.append(result_id)
        steps = [
            {
                "name": METRIC_LABELS.get(key, key),
                "status": "passed" if bool(value) else "failed",
                "stage": "finished",
                "start": 0,
                "stop": 0,
            }
            for key, value in row.items()
            if isinstance(value, bool)
        ]
        write(
            f"{result_id}-result.json",
            {
                "uuid": result_id,
                "name": row["sample_id"],
                "fullName": f"ai-review.{row['sample_id']}",
                "status": "passed" if row["task.reason_codes_matched"] else "failed",
                "stage": "finished",
                "start": 0,
                "stop": 0,
                "labels": [
                    {"name": "layer", "value": "task"},
                    {"name": "judge", "value": report.judge_engine},
                ],
                "parameters": [
                    {"name": "工具序列匹配", "value": str(row["tools.sequence_match"])},
                    {"name": "参数正确", "value": str(row["tools.parameter_ok"])},
                    {"name": "步数", "value": str(row["tools.steps"])},
                ],
                "steps": steps,
                "attachments": [],
            },
        )

    # 整体门禁作为一个单独用例，红/绿一眼可见
    gate_id = uuid.uuid4().hex
    children.append(gate_id)
    write(
        f"{gate_id}-result.json",
        {
            "uuid": gate_id,
            "name": "baseline 回归门禁",
            "fullName": "ai-review.regression-gate",
            "status": "passed" if report.passed else "failed",
            "stage": "finished",
            "start": 0,
            "stop": 0,
            "steps": [
                {
                    "name": alert.describe(),
                    "status": "failed",
                    "stage": "finished",
                    "start": 0,
                    "stop": 0,
                }
                for alert in report.alerts
            ],
            "attachments": [],
        },
    )

    live_id = uuid.uuid4().hex
    children.append(live_id)
    live_requested = report.execution.get("mode") == "live"
    write(f"{live_id}-result.json", {
        "uuid": live_id,
        "name": "真实 AI 服务验证（独立于 baseline）",
        "fullName": "ai-review.live-validation",
        "status": ("passed" if report.execution.get("live_validation_passed") else "failed") if live_requested else "skipped",
        "stage": "finished", "start": 0, "stop": 0,
        "statusDetails": {"message": json.dumps(report.execution, ensure_ascii=False)},
    })

    write(
        f"{parent}-container.json",
        {
            "uuid": parent,
            "name": "审核 AI 评测",
            "children": children,
            "befores": [],
            "afters": [],
            "start": 0,
            "stop": 0,
        },
    )
    return len(children)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def _platform_verdict_fn(snapshot_path=None, *, use_snapshot: bool = True):
    """真实平台规则引擎的结论函数；拿不到平台依赖时返回 None（退回占位推定）。

    惰性 import：``ai_service`` 平时不依赖 ``app`` 的 FastAPI / OpenCV，
    只有真正要跑平台口径时才引进来。

    **字段输入优先走 OCR 快照**（CTE-2）。这是本阶段的核心改动：
    此前 ``fields`` 一律来自 ``labels.json`` 的标注真值，
    于是「字段全部解析成功」恒成立、这个信号没有区分度；
    改用真实 PaddleOCR 的观测之后，``missing_*`` 与误识别才会真的出现。
    快照缺失或未命中时回退标注真值，并在报告里如实标注来源。
    """
    try:
        from ai_service.eval.platform_rules import platform_verdict
        from test_evolution.ocr_snapshot import SnapshotReplay, load
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        print(f"[评测] 平台规则引擎不可用（{exc}），结论层退回占位推定。", file=sys.stderr)
        return None

    replay = None
    if use_snapshot:
        snapshot = load(snapshot_path)
        if snapshot is None:
            print(
                "[评测] 没有 OCR 快照，字段回退到 labels.json 标注真值。\n"
                "       注意：此时「字段解析成功」恒成立，OCR/双判面的结论不可信。\n"
                "       录制：python -m scripts.record_ocr_snapshot",
                file=sys.stderr,
            )
        else:
            # strict=False：未命中不抛错，让评测跑完并在报告里报出未命中数。
            # 为什么不像 cassette 那样硬失败 —— cassette 未命中意味着「这条
            # 测试没真跑」，而快照未命中仍有标注真值这条退路，把它标出来
            # 比整轮崩掉更有用。未命中数会进报告。
            replay = SnapshotReplay(snapshot, strict=False)
            print(f"[评测] 字段来自 OCR 快照（{len(snapshot.observations)} 条观测，"
                  f"录制于 {snapshot.recorded_at}）")

    if replay is None:
        return platform_verdict

    def verdict_with_snapshot(sample):
        return platform_verdict(sample, snapshot=replay)

    verdict_with_snapshot.replay = replay  # 供调用方读未命中数
    return verdict_with_snapshot


def _platform_dual_judge_fn():
    """真实双判编排；拿不到平台依赖时返回 None（不测双判）。"""
    try:
        from ai_service.eval.platform_rules import platform_dual_judge
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        print(f"[评测] 双判编排不可用（{exc}），本轮不测双判。", file=sys.stderr)
        return None
    return platform_dual_judge


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)

    golden = load_golden_set(args.labels, target_size=args.target_size)
    if not golden.samples:
        print(f"golden 集为空，请检查标注文件：{args.labels}", file=sys.stderr)
        return 2

    llm = build_llm_client() if args.live else None
    if args.live and llm is not None and not llm.available:
        print("[--live] 模型未配置，真实 AI 验证失败；请配置模型或显式去掉 --live。", file=sys.stderr)
        return 2

    verdict_fn = _platform_verdict_fn(
        getattr(args, "ocr_snapshot", None),
        use_snapshot=not getattr(args, "no_ocr_snapshot", False),
    )

    report = asyncio.run(
        evaluate(
            golden,
            llm=llm,
            budget=AgentBudget(max_steps=args.max_steps),
            prefer_llm_judge=args.live,
            baseline_path=None if args.no_baseline else args.baseline,
            tolerance=args.tolerance,
            verdict_fn=verdict_fn,
            dual_judge_fn=_platform_dual_judge_fn(),
        )
    )

    if args.calibrate:
        report.calibration = asyncio.run(
            run_calibration(
                args.calibration_path,
                llm=llm,
                prefer_llm_judge=args.live,
            )
        )

    print_report(report)
    print_regression(report)
    print_calibration(report)

    if args.save_baseline:
        path = save_baseline(report, args.baseline)
        print(f"已写入新基线：{path}")
        print(f"  登记方向的指标：{len(METRIC_DIRECTIONS)} 项\n")

    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"完整报告：{args.json_path}")

    if args.allure:
        count = write_allure(report, args.allure_dir)
        print(f"Allure 结果：{args.allure_dir}（{count} 个用例）")
        print("  allure serve reports/allure-results 查看")

    return 0 if report.passed and (not args.live or report.execution.get("live_validation_passed")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
