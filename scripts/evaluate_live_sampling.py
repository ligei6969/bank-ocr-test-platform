"""多次采样 + 跨模型的真实模型评测 CLI。

用法（CMD，key 只从环境变量进，不写进任何文件）：

    set "LLM_API_KEY=sk-..."
    python -m scripts.evaluate_live_sampling --models deepseek-chat deepseek-reasoner --rounds 3

输出到 ``reports/live-sampling/<时间戳>/``：
  - ``sampling.json``  —— 完整分布报告（逐指标均值 / 标准差 / 极差）
  - ``summary.txt``    —— 人读的摘要

它和 ``scripts/evaluate_ai_review.py --live`` 的区别：那个跑**一轮**、
拿**单点数字**；这个跑**多轮 × 多模型**、拿**分布**。回答的是两个不同的问题：
「这条链路通不通」vs「这个数字能不能信」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from ai_service.eval.golden import load_golden_set
from ai_service.eval.sampling import (
    ModelConfig,
    SamplingReport,
    build_sampling_report,
    run_sampling,
)

DEFAULT_LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"
OUTPUT_ROOT = ROOT_DIR / "reports" / "live-sampling"

#: 预置模型配置。同一个 provider 下点名不同模型，是「跨模型对照」的
#: 最小可行版本 —— 换 provider 需要另一套 base_url，留 model@base_url 覆盖。
#:
#: 名单来自 2026-10-03 对 GET /models 的实测（DeepSeek 官方只开这两个）；
#: 项目文档里历史出现过的 ``deepseek-chat`` / ``deepseek-reasoner`` 已下线，
#: 沿用旧名会得到 400 而不是「模型不存在」，容易误判成配置问题。
PRESET_MODELS: dict[str, ModelConfig] = {
    "deepseek-flash": ModelConfig(
        name="deepseek-flash",
        provider="openai",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
    ),
    "deepseek-v4-pro": ModelConfig(
        name="deepseek-v4-pro",
        provider="openai",
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
    ),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--models",
        nargs="+",
        default=["deepseek-flash"],
        help="模型名（预置：deepseek-flash / deepseek-v4-pro），"
        "或 model@base_url 形式的自定义条目",
    )
    parser.add_argument("--rounds", type=int, default=3, help="每个模型独立采样的轮数")
    parser.add_argument(
        "--labels", type=Path, default=DEFAULT_LABELS_PATH, help="golden 标注文件"
    )
    parser.add_argument("--target-size", type=int, default=40, help="golden 集条数")
    parser.add_argument("--max-steps", type=int, default=6, help="Agent 步数预算")
    parser.add_argument("--output", type=Path, default=None, help="输出目录（默认按时间戳新建）")
    return parser.parse_args(argv)


def _resolve_model(token: str) -> ModelConfig:
    """把 ``--models`` 的一个条目解析成配置。

    支持 ``name``（取预置）与 ``model@base_url``（临时自定义，name 即 model）。
    """
    if token in PRESET_MODELS:
        return PRESET_MODELS[token]
    if "@" in token:
        model, base_url = token.split("@", 1)
        return ModelConfig(
            name=model, provider="openai", model=model, base_url=base_url
        )
    # 没预置也没 @：当作 DeepSeek 上的裸模型名
    return ModelConfig(
        name=token,
        provider="openai",
        model=token,
        base_url="https://api.deepseek.com",
    )


def _format_summary(report: SamplingReport) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("真实模型多次采样评测（分布，不是单点）")
    lines.append("=" * 72)
    lines.append(f"轮数：{report.rounds}")
    lines.append("")
    for model, spread in report.spreads.items():
        lines.append(f"── {model} ──")
        if not spread:
            lines.append("   （无共同指标 —— 检查各轮是否跑的同一评测）")
        for name, item in spread.items():
            stdev = "n/a" if item.stdev is None else f"{item.stdev:.4f}"
            lines.append(
                f"   {name:<28} mean={item.mean:.4f}  stdev={stdev:<8}"
                f" range={item.range:.4f}  n={len(item.values)}"
            )
        lines.append("")
    lines.append("── 说明 ──")
    for note in report.notes:
        lines.append(f"   · {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    if not api_key:
        print(
            "[live-sampling] 没有 LLM_API_KEY —— 本工具只做真实调用，"
            "不提供假模型路径。配置后再跑。",
            file=sys.stderr,
        )
        return 2

    golden = load_golden_set(args.labels, target_size=args.target_size)
    if not golden.samples:
        print(f"golden 集为空：{args.labels}", file=sys.stderr)
        return 2

    configs = [_resolve_model(token) for token in args.models]
    print(
        f"开始采样：{len(configs)} 个模型 × {args.rounds} 轮，"
        f"golden {len(golden.samples)} 条 —— 真实调用，会产生费用",
        flush=True,
    )

    # 平台判定函数沿用 evaluate_ai_review 的注入方式，
    # 这里不复制它的实现，直接 import 保持单一事实来源。
    from scripts.evaluate_ai_review import _platform_verdict_fn, _platform_dual_judge_fn

    verdict_fn = _platform_verdict_fn(None, use_snapshot=True)
    dual_judge_fn = _platform_dual_judge_fn()

    per_model: dict[str, list] = {}
    failures: list[str] = []
    for config in configs:
        print(f"\n▶ 模型 {config.name}（{config.model} @ {config.base_url}）", flush=True)
        try:
            results = asyncio.run(
                run_sampling(
                    golden,
                    config,
                    api_key=api_key,
                    rounds=args.rounds,
                    verdict_fn=verdict_fn,
                    dual_judge_fn=dual_judge_fn,
                    max_steps=args.max_steps,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 失败要如实记录，不能吞
            failures.append(f"{config.name}: {type(exc).__name__}: {exc}")
            print(f"   ✗ 失败：{exc}", flush=True)
            continue
        per_model[config.name] = results
        for result in results:
            print(f"   ✓ 第 {result.round_index + 1} 轮完成", flush=True)

    if not per_model:
        print("\n所有模型都失败了，没有可报告的采样结果：", file=sys.stderr)
        for item in failures:
            print(f"   - {item}", file=sys.stderr)
        return 1

    report = build_sampling_report(
        args.rounds,
        per_model,
        notes=[
            f"模型配置：{', '.join(c.name for c in configs)}",
            f"golden 集来源：{args.labels}",
        ]
        + ([f"失败的模型：{'; '.join(failures)}"] if failures else []),
    )

    output = args.output or (
        OUTPUT_ROOT / f"{datetime.now():%Y%m%d-%H%M%S}-{'-'.join(c.name for c in configs)}"
    )
    output.mkdir(parents=True, exist_ok=True)

    (output / "sampling.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = _format_summary(report)
    (output / "summary.txt").write_text(summary + "\n", encoding="utf-8")

    print()
    print(summary)
    print(f"\n完整报告：{output / 'sampling.json'}")
    print(f"摘要：{output / 'summary.txt'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
