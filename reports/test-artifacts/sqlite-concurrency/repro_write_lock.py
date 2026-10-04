"""复现 SQLite 写锁竞争：并发调用 save_review_record，统计 OperationalError。

这是压测报告 [20260929-压力测试报告.md] 结论 1 的最小复现：
不经过 HTTP，直接压落库路径，用来在修复前后给出可比数字。

用法：
    python reports/test-artifacts/sqlite-concurrency/repro_write_lock.py --writers 40
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT_DIR))


def run_round(*, writers: int, per_writer: int, db_path: Path) -> dict:
    os.environ["REVIEW_RECORDS_DB_PATH"] = str(db_path)

    from app.review_records import save_review_record

    errors: list[str] = []
    succeeded = [0]
    lock = threading.Lock()
    start = threading.Barrier(writers)

    def worker(index: int) -> None:
        start.wait()
        for sequence in range(per_writer):
            try:
                save_review_record(
                    request_id=f"repro-{writers}-{index}-{sequence}",
                    doc_type="bank_card",
                    filename=f"a{index}.png",
                    ocr_mode="mock",
                    review_result="pass",
                    quality_result="pass",
                    quality_reasons=[],
                    fields={"card_number": "6222020000000000"},
                )
            except sqlite3.OperationalError as exc:
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001 - 复现脚本要看到全部异常类型
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")
            else:
                with lock:
                    succeeded[0] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(writers)]
    began = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - began

    total = writers * per_writer
    locked = sum(1 for item in errors if "database is locked" in item)
    return {
        "writers": writers,
        "total": total,
        "succeeded": succeeded[0],
        "failed": len(errors),
        "failed_rate": round(len(errors) / total, 4),
        "locked": locked,
        "elapsed_s": round(elapsed, 2),
        "sample_errors": sorted(set(errors))[:5],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--writers", type=int, nargs="+", default=[8, 40, 80])
    parser.add_argument("--per-writer", type=int, default=12)
    args = parser.parse_args()

    results = []
    for writers in args.writers:
        with tempfile.TemporaryDirectory(prefix="sqlite-repro-") as tmp:
            db_path = Path(tmp) / "review_records.db"
            outcome = run_round(
                writers=writers, per_writer=args.per_writer, db_path=db_path
            )
            results.append(outcome)
            print(
                f"writers={writers:>3}  total={outcome['total']:>5}  "
                f"failed={outcome['failed']:>3} ({outcome['failed_rate']:.2%})  "
                f"locked={outcome['locked']:>3}  {outcome['elapsed_s']}s"
            )
            for sample in outcome["sample_errors"]:
                print(f"           e.g. {sample}")

    worst = max((item["failed_rate"] for item in results), default=0.0)
    print(f"\nworst failure rate: {worst:.2%}")
    return 1 if worst > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
