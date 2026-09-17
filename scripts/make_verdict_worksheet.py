"""生成人工结论标注的「工作表」，让人只需要填一列。

为什么要这个脚本
----------------
`data/annotations/review_verdicts.json` 是评测里**唯一必须由人产出**的输入：
原因码的期望值能从注入的退化类型推出来，但 `pass / review / reject`
是业务判断，推不出来。用代码推一份再拿它当基准，等于系统自己给自己打分。

代码能做的是把手工业余量压到最小：把所有判断依据一次性摆好，
标注人只填 `expected_verdict` 一列。省掉的是「翻样本、拼字段、对齐 id」这些
机械工作，留下的是真正需要人做的那件事。

用法::

    # 生成工作表（默认 40 条；已有文件时拒绝覆盖）
    python -m scripts.make_verdict_worksheet

    # 覆盖已有文件
    python -m scripts.make_verdict_worksheet --force

    # 走到上限 50 条，并指定输出位置
    python -m scripts.make_verdict_worksheet --size 50 --out data/annotations

产出两个文件：

* ``review_verdicts.json`` —— 程序读的标注文件（`expected_verdict` 留空等你填）
* ``review_verdicts_worksheet.csv`` —— 用 Excel / WPS 填的表，填完把那一列
  抄回 JSON 即可（或者只填 CSV，让脚本读起来 —— 但 JSON 才是权威格式）

为什么不给「参考结论」
----------------------
默认**不**把系统当前的判定写进工作表。一旦标注人看见「系统判的是 review」，
他会倾向于写 review，于是这个基准就变成了「系统与自己的一致率」——
数字很好看，但什么都没测到。想对照看可以用 ``--with-reference``，
但报告里的结论不能拿那份数字说事。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ai_service.eval.golden import (  # noqa: E402
    DEFAULT_LABELS_PATH,
    MAX_GOLDEN_SIZE,
    build_golden_set,
)
from ai_service.eval.verdicts import (  # noqa: E402
    DEFAULT_VERDICTS_PATH,
    VERDICTS_FORMAT_VERSION,
    VALID_VERDICTS,
)

WORKSHEET_NAME = "review_verdicts_worksheet.csv"

INSTRUCTIONS = (
    "按监管口径判断每条记录**应当**得到什么结论：pass（无需人工）/ "
    "review（需人工复核）/ reject（拒绝）。只看左边给出的判断依据，"
    "不要参考系统当前的判定。填完在文件头部补上 annotated_by 与 annotated_at。"
)

COLUMNS_BASE: tuple[str, ...] = (
    "sample_id",
    "doc_type",
    "注入的退化类型",
    "原因码",
    "图片路径",
    "expected_verdict_填这里",
    "key_reason_codes_可选",
    "note_可选",
)

#: 参考列。默认不输出 —— 理由见模块开头。
COLUMNS_REFERENCE: tuple[str, ...] = ("参考_按原因码推导的结论",)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.make_verdict_worksheet",
        description="生成人工审核结论标注的工作表（JSON + CSV）",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=40,
        help=f"样本条数，默认 40，上限 {MAX_GOLDEN_SIZE}",
    )
    parser.add_argument(
        "--out",
        default="data/annotations",
        help="输出目录，默认 data/annotations",
    )
    parser.add_argument(
        "--labels",
        default=str(DEFAULT_LABELS_PATH),
        help="来源标注文件，默认 data/annotations/labels.json",
    )
    parser.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    parser.add_argument(
        "--with-reference",
        action="store_true",
        help="额外输出一列「系统当前判定」作为对照（会污染基准，报告里不能引用）",
    )
    return parser.parse_args(argv)


def build_rows(
    samples: Sequence[Any],
    *,
    with_reference: bool = False,
) -> List[Dict[str, str]]:
    """把 golden 样本转成工作表行。纯函数，方便单测。"""
    rows: List[Dict[str, str]] = []
    for sample in samples:
        row: Dict[str, str] = {
            "sample_id": sample.sample_id,
            "doc_type": sample.doc_type,
            "注入的退化类型": sample.quality_type,
            "原因码": "、".join(sample.expected_reason_codes) or "（无）",
            "图片路径": sample.image_path,
            "expected_verdict_填这里": "",
            "key_reason_codes_可选": "",
            "note_可选": "",
        }
        if with_reference:
            row["参考_按原因码推导的结论"] = sample.expected_quality_result
        rows.append(row)
    return rows


def build_payload(samples: Sequence[Any]) -> Dict[str, Any]:
    """生成待填的 JSON 骨架。

    ``expected_verdict`` 留空字符串而不是猜一个：空值会被
    :mod:`ai_service.eval.verdicts` 明确报成「还没填」，
    而猜一个值会被当成标注读进去。
    """
    return {
        "version": VERDICTS_FORMAT_VERSION,
        "_instructions": INSTRUCTIONS,
        "_allowed_verdicts": list(VALID_VERDICTS),
        "annotated_by": "",
        "annotated_at": "",
        "records": [
            {
                "sample_id": sample.sample_id,
                "expected_verdict": "",
                "key_reason_codes": [],
                "note": "",
            }
            for sample in samples
        ],
    }


def write_worksheet(rows: Sequence[Dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(COLUMNS_BASE) + (
        list(COLUMNS_REFERENCE) if rows and COLUMNS_REFERENCE[0] in rows[0] else []
    )
    # utf-8-sig：Excel 打开中文 CSV 不加 BOM 会乱码，这个坑每次都要踩一下
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out)
    verdicts_path = out_dir / DEFAULT_VERDICTS_PATH.name
    worksheet_path = out_dir / WORKSHEET_NAME

    if verdicts_path.exists() and not args.force:
        print(f"已存在 {verdicts_path}，未覆盖（要覆盖加 --force）", file=sys.stderr)
        return 2

    size = max(1, min(args.size, MAX_GOLDEN_SIZE))
    samples = build_golden_set(Path(args.labels), target_size=size)
    if not samples:
        print("没有从 labels.json 里抽出任何样本，检查 --labels 路径", file=sys.stderr)
        return 1

    verdicts_path.parent.mkdir(parents=True, exist_ok=True)
    verdicts_path.write_text(
        json.dumps(build_payload(samples), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_worksheet(build_rows(samples, with_reference=args.with_reference), worksheet_path)

    print(f"已生成待填标注文件：{verdicts_path}（{len(samples)} 条）")
    print(f"已生成填表用工作簿：{worksheet_path}")
    print()
    print("下一步（全部手工，约 1 小时）：")
    print("  1. 用 Excel / WPS 打开 CSV，只填 expected_verdict_填这里 一列")
    print(f"       取值只能是：{' / '.join(VALID_VERDICTS)}")
    print("     " + INSTRUCTIONS)
    print("  2. 把填好的那一列抄回 JSON 的 records[*].expected_verdict")
    print("     并在 JSON 头部补 annotated_by（谁标的）与 annotated_at（什么时候）")
    print("  3. 校验：python -m scripts.evaluate_ai_review")
    print("     报告里那行「决策层不可用」会消失，任务层多出 结论正确率")
    print()
    print("门槛：至少 20 条且覆盖 golden 集的 50%，否则仍按不可用处理 ——")
    print("一个基于 3 条样本的正确率比没有数字更糟，因为它看起来像结论。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
