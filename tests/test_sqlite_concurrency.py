"""SQLite 并发写入的回归测试。

背景：压测在 40 / 80 并发下复现 ``sqlite3.OperationalError: database is locked``
（见 ``reports/test-artifacts/pressure/``）。原因是每条审核请求都会在插入前
重跑一遍建表 / 建索引，于是几十个线程去抢同一个写锁。

这里的断言**刻意落在机制上，而不是只跑一遍并发看会不会红**：
「并发会不会失败」受机器、调度和时长影响，可能这次绿下次红；
而「建库只跑一次」「写连接互斥」「读不被写队列挡住」是确定性的，
改坏了就一定红。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.sqlite_connection import (
    connect_database,
    enable_wal_mode,
    normalize_database_path,
    setup_database_once,
)
from app.users import initialize_user_database
from app.review_records import (
    get_review_record,
    initialize_review_database,
    list_review_records,
    save_review_record,
)


def _record_fields() -> dict[str, str]:
    return {"card_number": "6222020202020001", "valid_date": "08/29", "name": "JB"}


def _insert(review_db: Path, request_id: str) -> None:
    save_review_record(
        request_id=request_id,
        doc_type="bank_card",
        filename="concurrency.png",
        ocr_mode="mock",
        review_result="pass",
        quality_result="pass",
        quality_reasons=[],
        fields=_record_fields(),
    )


# ── 机制：建库只跑一次 ────────────────────────────────────────────────────────

def test_setup_database_once_runs_setup_only_once(tmp_path: Path) -> None:
    """第二次调用不再执行建库语句 —— 这是压测修复的核心。"""
    database_path = tmp_path / "once.db"
    calls: list[int] = []

    def setup(connection: sqlite3.Connection) -> None:
        calls.append(1)
        connection.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")

    assert setup_database_once(database_path, key="k", setup=setup) is True
    assert setup_database_once(database_path, key="k", setup=setup) is False
    assert len(calls) == 1

    with connect_database(database_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "t" in names


def test_setup_database_once_reruns_after_database_file_is_removed(
    tmp_path: Path,
) -> None:
    """库文件被删掉后必须重建。

    只记「这个路径建过库」而不管文件还在不在，重建的空库就会缺表，
    调用方拿到的是 ``no such table``，而不是「建库没跑」。
    """
    database_path = tmp_path / "recreated.db"
    calls: list[int] = []

    def setup(connection: sqlite3.Connection) -> None:
        calls.append(1)
        connection.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")

    assert setup_database_once(database_path, key="k", setup=setup) is True
    database_path.unlink()
    assert setup_database_once(database_path, key="k", setup=setup) is True
    assert len(calls) == 2


def test_review_and_user_schema_setup_are_independent(tmp_path: Path, monkeypatch) -> None:
    """两张表共用一个库文件，各自的建库标记不能互相顶掉。"""
    database_path = tmp_path / "shared.db"
    monkeypatch.setenv("REVIEW_RECORDS_DB_PATH", str(database_path))

    initialize_review_database()
    initialize_user_database()

    with connect_database(database_path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"review_records", "users"} <= names


def test_normalize_database_path_is_case_and_separator_insensitive(
    tmp_path: Path,
) -> None:
    """写锁以路径为 key，同一文件的两种写法必须映射到同一个 key。"""
    database_path = tmp_path / "norm.db"
    variants = {
        str(database_path),
        str(database_path).replace("\\", "/"),
        str(database_path).upper(),
        str(database_path).lower(),
    }
    assert len({normalize_database_path(Path(item)) for item in variants}) == 1


# ── 机制：写连接互斥、读不被写队列挡住 ────────────────────────────────────────

def test_write_connections_are_mutually_exclusive(tmp_path: Path) -> None:
    """进程内写连接必须排队：同时只有一个线程持有写连接。

    这里不统计耗时，而是统计**重叠**：只要两个线程同时进入写连接，
    计数就会超过 1。改坏排队机制时这条一定红。
    """
    database_path = tmp_path / "exclusive.db"
    overlaps: list[int] = []
    active = 0
    guard = threading.Lock()

    def writer() -> None:
        nonlocal active
        with connect_database(database_path, write=True) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
            with guard:
                active += 1
                if active > 1:
                    overlaps.append(active)
            time.sleep(0.01)
            with guard:
                active -= 1

    threads = [threading.Thread(target=writer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert overlaps == []


def test_read_only_connection_does_not_wait_for_the_write_queue(
    tmp_path: Path,
) -> None:
    """只读连接不排队。

    如果所有连接都去抢写锁，WAL 下的读并发就白开了 —— 读会被写队列
    逐个串起来。这条断言守的是「读路径没有拿写锁」这个设计。
    """
    database_path = tmp_path / "read-while-write.db"
    with connect_database(database_path, write=True) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")

    holding = threading.Event()
    release = threading.Event()

    def hold_write_lock() -> None:
        with connect_database(database_path, write=True) as connection:
            connection.execute("INSERT INTO t (id) VALUES (1)")
            holding.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert holding.wait(timeout=10), "写线程没能拿到写连接"

    try:
        # 关键断言是「这条读能跑完」。写事务还没提交，所以读到 0 行是对的 ——
        # 这里不能断言行数，那测的是隔离级别而不是排队行为。
        with connect_database(database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        assert isinstance(count, int)
    finally:
        release.set()
        holder.join(timeout=10)

    assert not holder.is_alive()

    # 写事务提交后才能看到那一行。
    with connect_database(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


# ── 机制：新库使用 WAL ────────────────────────────────────────────────────────

def test_new_database_is_switched_to_wal(tmp_path: Path) -> None:
    """WAL 写在库文件头里，是持久属性，所以设置一次之后一直生效。"""
    database_path = tmp_path / "wal.db"

    with connect_database(database_path, write=True) as connection:
        assert enable_wal_mode(connection) == "wal"

    with connect_database(database_path) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


# ── 端到端：落库不再每请求跑 DDL，且并发写全部落库 ────────────────────────────

def test_saving_a_record_does_not_rerun_schema_setup(
    tmp_path: Path, monkeypatch
) -> None:
    """核心回归：读 / 写多次，建库语句只跑一次。

    修复前 ``initialize_review_database()`` 挂在每个读写之前，
    每次都真的执行建表 / 建索引 —— 这正是并发下锁风暴的来源。
    """
    database_path = tmp_path / "no-ddl.db"
    monkeypatch.setenv("REVIEW_RECORDS_DB_PATH", str(database_path))

    import app.review_records as review_records

    calls: list[int] = []
    original = review_records._create_review_schema

    def counting_setup(connection: sqlite3.Connection) -> None:
        calls.append(1)
        original(connection)

    monkeypatch.setattr(review_records, "_create_review_schema", counting_setup)

    _insert(database_path, "no-ddl-1")
    _insert(database_path, "no-ddl-2")
    _insert(database_path, "no-ddl-3")
    assert get_review_record("no-ddl-1") is not None
    assert len(list_review_records()) == 3

    assert len(calls) == 1


def test_read_path_still_creates_schema_for_a_fresh_database(
    tmp_path: Path, monkeypatch
) -> None:
    """行为保持：对空库直接查询仍然建表并返回空结果，而不是 ``no such table``。"""
    database_path = tmp_path / "fresh-read.db"
    monkeypatch.setenv("REVIEW_RECORDS_DB_PATH", str(database_path))

    assert get_review_record("missing") is None
    assert list_review_records() == []


def test_concurrent_review_saves_all_persist(tmp_path: Path, monkeypatch) -> None:
    """并发落库：零异常，且成功返回的每一条都真的写进了库。"""
    database_path = tmp_path / "concurrent.db"
    monkeypatch.setenv("REVIEW_RECORDS_DB_PATH", str(database_path))

    writers = 24
    per_writer = 5
    errors: list[str] = []
    guard = threading.Lock()
    start = threading.Barrier(writers)

    def worker(index: int) -> None:
        start.wait(timeout=30)
        for sequence in range(per_writer):
            try:
                _insert(database_path, f"concurrent-{index}-{sequence}")
            except Exception as exc:  # noqa: BLE001 - 要把任何异常类型都记下来
                with guard:
                    errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert not [item for item in errors if "database is locked" in item]

    with connect_database(database_path) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM review_records").fetchone()[0]
    assert rows == writers * per_writer


def test_write_queue_serializes_contention_instead_of_failing(
    tmp_path: Path, monkeypatch
) -> None:
    """高并发下不出现 ``database is locked``。

    与上一条的区别：这里每个线程在同一个写事务里多做一些工作，把写锁
    持有时间拉长，专门制造竞争窗口。修复前这类写法会等到 busy timeout；
    现在应该在排队里等，而不是失败。
    """
    database_path = tmp_path / "contended.db"
    monkeypatch.setenv("REVIEW_RECORDS_DB_PATH", str(database_path))

    writers = 16
    errors: list[str] = []
    guard = threading.Lock()
    start = threading.Barrier(writers)

    def worker(index: int) -> None:
        start.wait(timeout=30)
        try:
            with connect_database(database_path, write=True) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
                for sequence in range(20):
                    connection.execute(
                        "INSERT INTO t (id) VALUES (?)", (index * 100 + sequence,)
                    )
        except Exception as exc:  # noqa: BLE001
            with guard:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []

    with connect_database(database_path) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert rows == writers * 20
