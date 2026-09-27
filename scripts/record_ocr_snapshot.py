"""录制 / 校验 OCR 快照。**这个脚本会加载真实 PaddleOCR，不进 CI。**

用法::

    # 录制 golden 集用到的那些图（默认，推荐）
    python -m scripts.record_ocr_snapshot

    # 先看看会录什么，不写文件
    python -m scripts.record_ocr_snapshot --dry-run

    # 录全量样本（2100 张，很慢）
    python -m scripts.record_ocr_snapshot --all

    # 校验：重新跑一遍，和已批准的快照 diff（**不覆盖**）
    python -m scripts.record_ocr_snapshot --verify

    # 确认 diff 可接受后，显式更新快照
    python -m scripts.record_ocr_snapshot --update

为什么 ``--verify`` 不自动覆盖
------------------------------
如果某天 PaddleOCR 升了版本把结果跑坏了，自动更新 baseline 的系统会
非常勤劳地把坏结果记成新基准，然后宣布「所有测试都通过」。

所以流程是：**录制 → diff → 人工 review → 显式更新**。
``--verify`` 退出码非零表示有差异，交给 CI 的 live job 去报警。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

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

from test_evolution.ocr_snapshot import (  # noqa: E402
    DEFAULT_SNAPSHOT_PATH,
    OcrSnapshot,
    load,
    record,
    save,
    verify_unchanged,
)

LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"

#: 录制的目标集 = golden 集的全部图。默认 40 条样本，
#: 2100 张全录在 CPU 上要跑很久，而评测只用得到这 40 条。
MAX_GOLDEN_TARGET = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_targets(*, everything: bool = False) -> List[Tuple[Path, str]]:
    """挑出要录的图，返回 ``[(路径, doc_type)]``。

    **必须是 golden 集真正用到的那批图**，不能另用一套选取策略 ——
    否则 CI 回放时会出现「快照里没有这张图」的未命中。
    这个坑实测踩到过：按桶排序取前 N 张会取到 ``back/blur/0001..0004``，
    而 golden 的 ``_balanced_take`` 是**按面轮转**的，它要的是
    ``back/blur/0001`` 和 ``front/blur/0001``。

    所以这里直接复用 ``ai_service.eval.golden.build_golden_set`` ——
    「录什么」由「评测用什么」决定，两个策略不该各写一份。
    """
    from ai_service.eval.golden import build_golden_set

    seen: Dict[str, str] = {}
    for sample in build_golden_set(target_size=MAX_GOLDEN_TARGET):
        if sample.image_path:
            seen.setdefault(sample.image_path, sample.doc_type)

    if everything:
        # --all：把 labels.json 里其余样本也录进来（替代输入还没实现的
        # 「降质样本过 Paddle」）。这一档很慢，用于 CTE-2 的后续工作。
        labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
        for item in labels:
            raw_doc = str(item.get("doc_type") or "")
            doc_type = "bank_card" if raw_doc == "bank_card" else (
                "id_card" if raw_doc.startswith("id_card") else ""
            )
            if doc_type:
                seen.setdefault(str(item["image_path"]), doc_type)

    return [(ROOT_DIR / path, doc_type) for path, doc_type in sorted(seen.items())]


def _diff(old: OcrSnapshot, new: OcrSnapshot) -> Dict[str, List[str]]:
    """比较两份快照，按「发生了什么」分组。

    分组而不是只给一个布尔：快照 diff 的真实价值是回答
    **「哪几条样本的结论因为输入真实化而变了」**，而不是「变了没有」。
    """
    changes: Dict[str, List[str]] = {
        "新增": [],
        "消失": [],
        "OCR 文本变化": [],
        "解析字段变化": [],
        "质量结果变化": [],
        "内容哈希变化": [],
    }
    for key in sorted(set(old.observations) | set(new.observations)):
        before = old.observations.get(key)
        after = new.observations.get(key)
        if before is None:
            changes["新增"].append(key)
            continue
        if after is None:
            changes["消失"].append(key)
            continue
        if before.ocr_texts != after.ocr_texts:
            changes["OCR 文本变化"].append(key)
        if before.parsed_fields != after.parsed_fields:
            changes["解析字段变化"].append(key)
        if before.quality.get("quality_result") != after.quality.get("quality_result"):
            changes["质量结果变化"].append(key)
        if before.image_sha256 != after.image_sha256:
            changes["内容哈希变化"].append(key)
    return {name: items for name, items in changes.items() if items}


def cmd_record(args: argparse.Namespace) -> int:
    targets = load_targets(everything=args.all)
    print(f"[录制] 目标 {len(targets)} 张图，模式 {args.mode}")

    if args.dry_run:
        for path, doc_type in targets[:20]:
            print(f"  {doc_type:<10} {path.relative_to(ROOT_DIR)}")
        if len(targets) > 20:
            print(f"  ... 还有 {len(targets) - 20} 张")
        print("[录制] --dry-run，未写文件。")
        return 0

    # 断点：全量 2100 张要跑近半小时，而 PaddleOCR 在长循环里会崩
    # （实测踩到 CUDA error(700)）。边录边写，崩了重跑接着来。
    checkpoint = None if args.no_checkpoint else args.output
    snapshot = record(
        (path for path, _ in targets),
        doc_type_of=lambda path: dict(targets)[path],
        ocr_mode=args.mode,
        recorded_at=_now(),
        checkpoint_path=checkpoint,
        resume=not args.restart,
        notes=[
            "由 scripts/record_ocr_snapshot.py 录制。",
            "这是**系统观测**（引擎实际看到了什么），不是 Ground Truth —— "
            "Ground Truth 在 labels.json。",
            f"覆盖 {len(targets)} 张图，OCR 模式 {args.mode}。",
        ],
    )

    failed = [k for k, obs in snapshot.observations.items() if obs.parse_error]
    if failed:
        print(f"[录制] 警告：{len(failed)} 张有解析/质量错误（已记入 parse_error）")

    save(snapshot, args.output)
    print(f"[录制] 已写入 {args.output}（{len(snapshot.observations)} 条观测）")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    approved = load(args.output)
    if approved is None:
        print(f"[校验] 没有已批准的快照（{args.output}）。先跑一次录制。", file=sys.stderr)
        return 2

    stale = verify_unchanged(approved)
    if stale:
        print(f"[校验] {len(stale)} 张图的内容和快照记录的不一致：")
        for key in stale:
            print(f"  ! {key}")
        print("       图被重新生成过而快照没更新 —— 回放出来的是旧图的观测。")

    print(f"[校验] 重跑真实 OCR（{len(approved.observations)} 张）...")
    fresh = record(
        (ROOT_DIR / key for key in sorted(approved.observations)),
        doc_type_of=lambda path: _doc_type_for(approved, path),
        ocr_mode=args.mode,
        recorded_at=_now(),
    )

    changes = _diff(approved, fresh)
    if not changes:
        print("[校验] 无差异。快照与当前真实 OCR 行为一致。")
        return 0

    print("\n[校验] 发现差异：")
    for name, items in changes.items():
        print(f"\n  {name}（{len(items)}）：")
        for key in items[:10]:
            print(f"    - {key}")
        if len(items) > 10:
            print(f"    ... 还有 {len(items) - 10} 条")

    print(
        "\n[校验] 快照**未被覆盖**。确认差异可接受后再显式执行：\n"
        "        python -m scripts.record_ocr_snapshot --update"
    )
    return 1


def _doc_type_for(snapshot: OcrSnapshot, path: Path) -> str:
    key = str(path).replace("\\", "/")
    marker = "data/"
    index = key.find(marker)
    if index > 0:
        key = key[index:]
    obs = snapshot.observations.get(key.lstrip("./"))
    return obs.doc_type if obs else ""


def cmd_update(args: argparse.Namespace) -> int:
    """显式更新快照 —— 与 ``--verify`` 分开，避免「校验顺手改基准」。"""
    print("[更新] 重新录制并覆盖快照...")
    args.dry_run = False
    result = cmd_record(args)
    if result != 0:
        return result
    print(
        "[更新] 完成。请把快照的 diff 写进 docs/baseline_migrations/ 并一起提交 ——\n"
        "       否则下一个看 diff 的人不知道基准为什么变了。"
    )
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="录制/校验 OCR 快照（需真实 PaddleOCR，不进 CI）"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    parser.add_argument("--mode", default="paddle", choices=["paddle", "mock"])
    parser.add_argument("--all", action="store_true", help="录全量样本（很慢）")
    parser.add_argument("--dry-run", action="store_true", help="只列目标，不写文件")
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="不写断点（整批录完才落盘）。默认边录边写，崩了可续",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="忽略已有断点，从头重录（默认续录）",
    )
    parser.add_argument("--verify", action="store_true", help="重跑并 diff，不覆盖")
    parser.add_argument("--update", action="store_true", help="显式覆盖快照")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.verify and args.update:
        print("--verify 与 --update 互斥：校验不该顺手改基准。", file=sys.stderr)
        return 2
    if args.verify:
        return cmd_verify(args)
    if args.update:
        return cmd_update(args)
    return cmd_record(args)


if __name__ == "__main__":
    raise SystemExit(main())
