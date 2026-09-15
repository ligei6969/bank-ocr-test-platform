"""``--live`` 冒烟入口的测试。

``--live`` 是唯一会真实出网的入口，所以它的**失败体验**比成功体验更重要：
配错 key 的人正在排错，这时候再抛一个未捕获异常只会让人更懵。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ai_service import __main__ as cli
from ai_service.llm import LLMClient, LLMUnavailableError
from ai_service.prompts import EXPLAIN_GENERATE, QUERY_REWRITE, RERANK


class ScriptedLLM:
    """一个按 prompt 关键词返回合法响应的假模型。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:scripted:test"

    async def complete(self, prompt: str, **_: Any) -> str:
        self.prompts.append(prompt)
        if "改写" in prompt:
            return '["为什么需要人工复核", "image_blur 是什么意思", "该怎么让用户重拍"]'
        if "排序" in prompt:
            return "[0, 1, 2, 3, 4]"
        return "这是模型生成的解释正文。"


# ── 没有模型时：清晰提示，不抛未捕获异常 ──────────────────────────────────────
# 注：清空 LLM 环境变量由 tests/conftest.py 的 autouse fixture 统一负责。

def test_live_without_api_key_explains_how_to_configure(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--live"])
    captured = capsys.readouterr()

    assert code == 2
    assert "没有可用的模型" in captured.err
    assert "LLM_API_KEY" in captured.err
    assert "LLM_BASE_URL" in captured.err
    assert "Traceback" not in captured.err
    # 没有配置就不该有任何结果输出
    assert captured.out.strip() == ""


def test_live_with_provider_none_also_reports_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("LLM_API_KEY", "sk-should-be-ignored")

    code = cli.main(["--live"])
    captured = capsys.readouterr()

    assert code == 2
    assert "LLM_PROVIDER=none" in captured.err
    assert "Traceback" not in captured.err


# ── 有模型时：跑通并回传 prompt 版本 ──────────────────────────────────────────

def test_live_reports_engine_and_prompt_versions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "build_llm_client", lambda: ScriptedLLM())

    code = cli.main(["--live"])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)

    assert payload["engine"]["generation"] == "llm"
    assert payload["degraded"] is False

    used = payload["prompt_versions"]["used"]
    # 核心不变量：报出来的 prompt 版本必须与「这一步到底用没用模型」严格一致。
    # 硬编码某一版是否出现会把测试绑死在检索召回数量上。
    engine = payload["engine"]
    assert ("query_rewrite" in used) == (engine["rewrite"] == "llm")
    assert ("rerank" in used) == (engine["rerank"] == "llm")
    assert ("explain_generate" in used) == (engine["generation"] == "llm")
    # 生成这一步一定是模型做的，所以它的版本一定在
    assert used["explain_generate"] == EXPLAIN_GENERATE.label

    assert "generation=llm" in captured.err
    assert "prompt 版本" in captured.err


def test_live_survives_a_failing_provider(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingLLM(ScriptedLLM):
        async def complete(self, prompt: str, **_: Any) -> str:
            raise LLMUnavailableError("上游 502")

    monkeypatch.setattr(cli, "build_llm_client", lambda: FailingLLM())

    code = cli.main(["--live"])
    captured = capsys.readouterr()

    # 模型挂了不该让链路崩：降级成模板仍然产出完整结果
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["degraded"] is True
    assert payload["explanation"]

    # 「调用过」和「成功」是两件事：调用过但失败的步骤要留痕，
    # 这告诉你哪一版 prompt 参与了这次失败；没轮到的步骤不该报版本。
    used = payload["prompt_versions"]["used"]
    assert used["query_rewrite"] == QUERY_REWRITE.label
    assert used["rerank"] == RERANK.label
    assert "explain_generate" not in used


# ── 默认路径必须离线 ─────────────────────────────────────────────────────────

def test_demo_makes_no_network_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """默认路径必须离线：把 urlopen 换成会炸的实现，``--demo`` 仍要跑通。

    只断言「没配 key 时 degraded 为真」是不够的 —— 那只说明结果降级了，
    不能说明链路没出网。直接掐掉网络出口才是真的证明。
    """
    import urllib.request

    def explode(*_: Any, **__: Any) -> Any:
        raise AssertionError("默认路径不应该发起任何网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    code = cli.main(["--demo"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["engine"]["llm_available"] is False
    assert payload["engine"]["generation"] == "template"
    assert payload["prompt_versions"]["used"] == {}


def test_demo_result_carries_the_registry_version(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--demo"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["prompt_versions"]["registry"]
    assert payload["prompt_versions"]["used"] == {}


def test_llm_client_protocol_is_not_extended_by_structured_output() -> None:
    """结构化输出是模块级函数，不往协议上加方法 —— 否则所有假模型都要改。"""
    assert not hasattr(LLMClient, "complete_json")
