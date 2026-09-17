"""SQLite 连接的生命周期：打开 →（提交 / 回滚）→ **一定关闭**。

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
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def connect_database(database_path: Path) -> Iterator[sqlite3.Connection]:
    """打开一个连接；正常退出则提交，抛异常则回滚，**两种情况都关闭**。

    与 ``with sqlite3.connect(...)`` 相比，唯一的行为差别是退出时一定
    ``close()``：不把句柄留给 GC，也就不再依赖「谁先被回收」这个偶然因素。
    提交 / 回滚的语义与原生写法保持一致。

    ⚠️ 连接上的 ``row_factory`` 被设为 ``sqlite3.Row``（与原先两个 store 的
    写法一致，它们需要 ``dict(row)``）。因此 ``fetchone()`` 返回的是
    ``sqlite3.Row`` 而**不是元组** —— 按列名或下标取值都可以，
    但要和元组比较得自己 ``tuple(row)``。这层差别曾经让两个测试静默地
    从「通过」变成「失败」，所以写在这里。
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=5.0)
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


__all__ = ("connect_database",)
