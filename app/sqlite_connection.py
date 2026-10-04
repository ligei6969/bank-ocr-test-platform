"""SQLite 连接的生命周期与并发写入策略：打开 →（提交 / 回滚）→ **一定关闭**。

为什么单独一个模块
------------------
``sqlite3.Connection`` 当上下文管理器用时的语义很容易记错：
``with sqlite3.connect(...)`` **只提交，不关闭**（CPython 文档「Connection 对象」
一节写明了这一点），文件句柄要等对象被回收才释放。在 POSIX 上这几乎看不出来，
在 Windows 上就直接表现为「进程还在跑，自己那个 db 文件删不掉、改不了」。

这不是理论问题。本项目的审核落库与用户表原先都写成
``with _connect() as connection:``，于是每次请求都攒下一个尚未关闭的句柄，
一直挂到 GC 才还 —— 这既是文件锁的来源，也是运行期的句柄抖动。

实测（Python 3.13 / Windows）：

* ``create_user()`` 返回后立刻 unlink 同一个 db 文件 → ``PermissionError [WinError 32]``；
* 先 ``gc.collect()`` 再 unlink → 成功，说明句柄只是「还没被回收」，不是永久占用；
* 原生 ``with sqlite3.connect(path) as c:`` 之后同样删不掉 → 确认 ``with`` 不关闭。

所以连接的开与关只在这里实现一份：**这是机制**（出错就是 bug，应该只有一份实现），
而「库放在哪」是策略，仍由各 store 自己决定。

并发写入：三个机制层的修正
--------------------------
压测（见 ``reports/test-artifacts/pressure/``）在 40 / 80 并发下复现了
``sqlite3.OperationalError: database is locked``，冒泡成 HTTP 500。
根因不是 SQLite 慢，而是**同一进程里几十个线程同时抢同一个写锁**，
而每个请求在写之前还要跑一遍建表 / 建索引：

* ``save_review_record`` 每次都先调 ``initialize_review_database()``，
  那是 2 条写语句（``CREATE TABLE`` / ``CREATE INDEX``）+ 1 条读；
* 在默认的 rollback journal 下，写事务要等到没有读者才拿得到排他锁，
  每次 commit 还要 fsync。

于是 80 个并发写者退化成「几十个线程 × 每请求 3 个写事务」的锁风暴，
部分线程等满 5 秒超时。这里在机制层做三件事：

1. **建库语句每个库文件只跑一次**（:func:`setup_database_once`）——
   把每请求的写事务从 3 个降到 1 个；
2. **写连接在进程内排队**（``connect_database(..., write=True)``）——
   SQLite 本来就只允许一个写者，排队把「抢锁 → 超时」换成「先进先出」；
3. **WAL + 更长的 busy timeout** —— 读不再阻塞写，写不再阻塞读。

刻意**没有**把 ``synchronous`` 降成 ``NORMAL``：这是一个审核审计库，
拿「掉电可能丢最后几条记录」换吞吐不在本项目的取舍范围内。
排队只解决**单进程内**的竞争；多 worker / 多进程部署仍要靠 SQLite 自己的
忙等待，这一点写在 ``docs/`` 的压力测试补充里，不假装已经解决。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterator, Set, Tuple

#: 抢不到锁时最多等多久（秒）。``sqlite3.connect`` 的 ``timeout`` 参数
#: 就是 SQLite 的 busy handler 时限，等价于 ``PRAGMA busy_timeout``。
#:
#: 进程内的竞争已由下面的写锁排队消掉，这个值只对**跨进程**竞争生效
#: （多 worker、外部脚本同时写同一个库）。5 秒在压测里不够，
#: 因为当时每次请求要抢 3 次写锁；现在每请求 1 次，留 30 秒只会影响
#: 真正的病态场景，正常路径根本走不到 busy handler。
DEFAULT_BUSY_TIMEOUT_S = 30.0

#: 每个库文件一把写锁。SQLite 一次只允许一个写者，所以在进程内排队
#: 不损失并发度，只是把「抢锁失败」换成「等前面的人写完」。
#:
#: 用 ``RLock`` 而不是 ``Lock``：将来若有代码在持有一个写连接的同时
#: 再开一个写连接，``Lock`` 会自己和自己死锁，而 ``RLock`` 允许同线程重入。
_WRITE_LOCKS: Dict[Path, threading.RLock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()

#: 已经跑过建库语句的 ``(库文件, 用途)``。见 :func:`setup_database_once`。
_SETUP_DONE: Set[Tuple[Path, str]] = set()
_SETUP_GUARD = threading.RLock()


def normalize_database_path(database_path: Path) -> Path:
    """把库路径规范化成**同一把锁的同一个 key**。

    写锁与建库标记都以路径为 key，所以 ``"J:/a/b.db"`` 与 ``"j:\\a\\b.db"``
    必须映射到同一个值，否则两个线程会各拿一把锁去写同一个文件，
    排队机制形同虚设。

    用 ``abspath`` 而不是 ``resolve``：``resolve`` 会去问文件系统（真实路径、
    符号链接），而这里只是要一个**字符串层面的唯一标识**，且库文件可能
    还不存在。``normcase`` 负责 Windows 的大小写不敏感。
    """
    return Path(os.path.normcase(os.path.abspath(str(database_path))))


def _write_lock_for(path: Path) -> threading.RLock:
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _WRITE_LOCKS[path] = lock
        return lock


def enable_wal_mode(connection: sqlite3.Connection) -> str:
    """把库切到 WAL，返回**实际生效**的 journal 模式。

    WAL 是写在库文件头里的**持久属性**，不是每连接设置，所以只在建库那一次
    调用即可。把它单独抽出来是为了能如实报告结果：部分文件系统（网络盘、
    某些容器挂载）不支持 WAL，此时 SQLite **静默返回旧模式**而不是报错 ——
    假定它一定成功，就会在排查并发问题时对着一个「以为开了 WAL」的库找原因。
    """
    try:
        row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    except sqlite3.DatabaseError:
        return "unknown"
    return str(row[0]) if row else "unknown"


@contextmanager
def connect_database(
    database_path: Path, *, write: bool = False
) -> Iterator[sqlite3.Connection]:
    """打开一个连接；正常退出则提交，抛异常则回滚，**两种情况都关闭**。

    与 ``with sqlite3.connect(...)`` 相比，唯一的行为差别是退出时一定
    ``close()``：不把句柄留给 GC，也就不再依赖「谁先被回收」这个偶然因素。
    提交 / 回滚的语义与原生写法保持一致。

    ``write=True`` 时先取得该库文件的进程内写锁，**直到 commit / rollback
    并关闭之后才释放** —— 锁必须覆盖提交，否则两个线程仍会在提交那一刻
    撞在一起。只读调用保持 ``write=False``（默认），这样 WAL 下读与写
    可以真正并行，不会被写队列拖住。

    ⚠️ 连接上的 ``row_factory`` 被设为 ``sqlite3.Row``（与原先两个 store 的
    写法一致，它们需要 ``dict(row)``）。因此 ``fetchone()`` 返回的是
    ``sqlite3.Row`` 而**不是元组** —— 按列名或下标取值都可以，
    但要和元组比较得自己 ``tuple(row)``。这层差别曾经让两个测试静默地
    从「通过」变成「失败」，所以写在这里。
    """
    path = normalize_database_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _open() -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(path, timeout=DEFAULT_BUSY_TIMEOUT_S)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    if not write:
        with _open() as connection:
            yield connection
        return

    with _write_lock_for(path):
        with _open() as connection:
            yield connection


def setup_database_once(
    database_path: Path,
    *,
    key: str,
    setup: Callable[[sqlite3.Connection], None],
    wal: bool = True,
) -> bool:
    """对同一个库文件、同一个 ``key``（一组表 / 索引）**只执行一次**建库语句。

    返回 ``True`` 表示这次真的执行了 ``setup``。

    为什么要按 key 而不是按路径：``review_records`` 与 ``users`` 共用同一个
    库文件，两张表是两次独立的建库，谁先跑都不会漏掉另一张。

    ``key`` 里带着「文件还在不在」的判断：库文件被删掉（测试 teardown
    会这么做）之后必须重来，否则重建的空库会缺表，而调用方拿到的是
    ``no such table`` 而不是「建库没跑」。
    """
    path = normalize_database_path(database_path)
    marker = (path, key)
    with _SETUP_GUARD:
        if marker in _SETUP_DONE and path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with connect_database(path, write=True) as connection:
            if wal:
                enable_wal_mode(connection)
            setup(connection)
        _SETUP_DONE.add(marker)
        return True


__all__ = (
    "DEFAULT_BUSY_TIMEOUT_S",
    "connect_database",
    "enable_wal_mode",
    "normalize_database_path",
    "setup_database_once",
)
