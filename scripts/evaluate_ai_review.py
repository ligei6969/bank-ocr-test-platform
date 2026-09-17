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
    "parameter_accuracy": "工具参数正确率",
    "avg_steps": "平均步数（越低越好）",
    "reason_code_match_rate": "原因码命中率（代理指标）",
    "evidence_rate": "有引用依据的比例",
    "action_rate": "有处置建议的比例",
    "escalation_accuracy": "转人工判定准确率",
    "verdict_accuracy": "结论正确率（人工标注）",
    "degraded_rate": "降级运行比例（越低越好）",
    "truncated_rate": "被预算截断比例（越低越好）",
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
    "成本层离线恒为 0：CI 与默认路径不调用模型。"
    "接入真实模型后用 --live 重跑，这一层才会有数字。"
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
        print(f"   通过（基线 {report.baseline_path}）")
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

def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)

    golden = load_golden_set(args.labels, target_size=args.target_size)
    if not golden.samples:
        print(f"golden 集为空，请检查标注文件：{args.labels}", file=sys.stderr)
        return 2

    llm = build_llm_client() if args.live else None
    if args.live and llm is not None and not llm.available:
        print("[--live] 没有可用的模型，已退回离线评测。", file=sys.stderr)

    report = asyncio.run(
        evaluate(
            golden,
            llm=llm,
            budget=AgentBudget(max_steps=args.max_steps),
            prefer_llm_judge=args.live,
            baseline_path=None if args.no_baseline else args.baseline,
            tolerance=args.tolerance,
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

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
