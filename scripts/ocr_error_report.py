"""从 OCR 快照生成字段错误率基线。

用法::

    # 生成报告（markdown + csv），打印摘要
    python -m scripts.ocr_error_report

    # 只打印摘要
    python -m scripts.ocr_error_report --quiet

    # 指定快照与输出位置
    python -m scripts.ocr_error_report --snapshot data/annotations/ocr_outputs.json \
        --out-md reports/ocr_field_error_rates.md

**这个脚本完全离线**，只读快照与标注文件 —— 不需要 PaddleOCR，
可以随时重跑。它回答的是「各退化类型下的字段错误率是多少」，
那是阈值决策的输入。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _make_stdout_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover
            pass


_make_stdout_utf8()

from test_evolution.ocr_report import (  # noqa: E402
    DEFAULT_LABELS_PATH,
    blocked_surface_evidence,
    build_report,
    render_csv,
    render_markdown,
)
from test_evolution.ocr_snapshot import DEFAULT_SNAPSHOT_PATH, load  # noqa: E402


def print_summary(buckets: Dict[str, Any], snapshot: Any) -> None:
    print("=" * 78)
    print(f"OCR 字段错误率基线（快照 {len(snapshot.observations)} 条观测）")
    print("=" * 78)
    evidence = blocked_surface_evidence(buckets)
    print(f"{'桶':<28} {'样本':>5} {'平均字段正确率':>14} {'字段缺失率':>12}")
    print("-" * 78)
    for key in sorted(evidence):
        item = evidence[key]
        accuracy = item["mean_field_accuracy"]
        missing = item["missing_rate"]
        acc_text = "—" if accuracy is None else f"{accuracy * 100:5.1f}%"
        miss_text = "—" if missing is None else f"{missing * 100:5.1f}%"
        print(f"{key:<28} {item['samples']:>5} {acc_text:>14} {miss_text:>12}")
    print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="从 OCR 快照生成字段错误率基线")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_PATH)
    parser.add_argument(
        "--out-md", type=Path, default=ROOT_DIR / "reports" / "ocr_field_error_rates.md"
    )
    parser.add_argument(
        "--out-csv", type=Path, default=ROOT_DIR / "reports" / "ocr_field_error_rates.csv"
    )
    parser.add_argument("--quiet", action="store_true", help="只打印摘要")
    args = parser.parse_args(argv)

    snapshot = load(args.snapshot)
    if snapshot is None:
        print(
            f"[报告] 没有快照（{args.snapshot}）。\n"
            "       先跑 python -m scripts.record_ocr_snapshot",
            file=sys.stderr,
        )
        return 2

    buckets = build_report(snapshot, labels_path=args.labels)
    if not buckets:
        print(
            "[报告] 快照里没有任何一条能对上标注 —— 检查快照与 labels.json 是否同源。",
            file=sys.stderr,
        )
        return 2

    print_summary(buckets, snapshot)

    if not args.quiet:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        args.out_md.write_text(
            render_markdown(buckets, snapshot=snapshot), encoding="utf-8"
        )
        args.out_csv.write_text(render_csv(buckets), encoding="utf-8")
        print(f"[报告] 已写出 {args.out_md}")
        print(f"[报告] 已写出 {args.out_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
