"""AI 服务测试的公共配置。

核心职责：**保证测试默认离线。**

``build_llm_client()`` 会读 ``os.environ``，所以开发机 shell 里只要配了
``LLM_API_KEY``，测试就会真的去打模型 —— 既慢、又烧钱、还不确定。
这里在测试层统一清掉，与平台侧的 ``isolate_ai_assist`` 是同一套路：
「要不要联网」由显式用例决定（``--live`` 冒烟），不由开发机的环境决定。

需要「剔除抖动字段再逐字比对」的用例，用同目录的
:mod:`ai_service.tests.volatile_fields`，不要从 conftest 里 import ——
项目有两个同名 conftest，谁先加载谁赢，import 会指向哪个取决于跑法。
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest

LLM_ENV_VARS = (
    "LLM_PROVIDER",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_TIMEOUT_S",
    "LLM_MAX_RETRIES",
)


@pytest.fixture(autouse=True)
def isolate_llm_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """清空 LLM 相关环境变量，让每个用例自己决定用哪个假客户端。"""
    for name in LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
    # 双保险：用例里如果有代码直接读 os.environ，也保证不会残留
    for name in LLM_ENV_VARS:
        os.environ.pop(name, None)
