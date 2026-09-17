"""剔除「天然会变」的字段，供「两次运行必须逐字一致」的用例复用。

为什么单独一个模块而不是放 conftest
------------------------------------
项目里有两个 conftest：``tests/conftest.py``（平台侧）和
``ai_service/tests/conftest.py``（AI 服务侧）。测试文件用
``from conftest import ...`` 时，**谁先被 pytest 加载谁就赢** ——
全量跑的时候 ``tests/conftest.py`` 赢，单独跑 ai_service 的时候
另一个赢，于是同一个 import 在两种跑法下指向不同文件。
这种「跑法不同结果不同」的坑最难查，所以把要复用的函数放进
一个名字唯一的普通模块，绕开这场竞争。

递归而不是列位置
----------------
耗时字段散落在 ``latency_ms``、``trace[*].latency_ms``、
``trace[*].tool_events[*].latency_ms``、``tools[*].avg_latency_ms``
好几层。列位置的写法漏掉哪一层，那一层的毫秒抖动就会随缘把
「两次运行必须逐字一致」的用例翻红 —— 偶发失败比稳定失败更耗人，
因为它看着像环境问题，不像代码问题。
"""

from __future__ import annotations

from typing import Any

#: 天然会变的键名标记。子串匹配，所以新增 ``avg_latency_ms`` 之类不必改这里。
VOLATILE_KEY_MARKERS = ("latency",)


def drop_volatile(node: Any) -> None:
    """就地递归删掉所有耗时类字段。"""
    if isinstance(node, dict):
        for key in [k for k in node if any(m in k for m in VOLATILE_KEY_MARKERS)]:
            node.pop(key, None)
        for value in node.values():
            drop_volatile(value)
    elif isinstance(node, list):
        for item in node:
            drop_volatile(item)


__all__ = ("VOLATILE_KEY_MARKERS", "drop_volatile")
