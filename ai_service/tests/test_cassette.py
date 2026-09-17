"""cassette 录制 / 回放的测试。

最关键的一条是「replay 未命中必须失败且不能联网」。
这条如果不成立，cassette 就只是「看起来在回放」—— CI 依然是随机的、要花钱的，
只是更难发现而已。所以这里不测「回放能返回内容」，而是重点测**失败行为**。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ai_service.agentkit import (
    LIVE,
    REPLAY,
    ScriptedPlanner,
    blurred_context,
    cassette_agent,
    decision,
    finish_step,
    run,
    search_step,
)
from ai_service.cassette import (
    CASSETTE_VERSION,
    Cassette,
    CassetteLLMClient,
    CassetteMissError,
    fingerprint,
    wrap_tool,
)
from ai_service.llm import HttpLLMClient, NullLLMClient
from ai_service.tool_manager import Tool

from volatile_fields import drop_volatile


def planner_script() -> List[Dict[str, Any]]:
    return [search_step(query="image_blur 释义"), finish_step("根因在影像质量层。")]


# ── 指纹 ──────────────────────────────────────────────────────────────────────

def test_fingerprint_is_stable_regardless_of_key_order() -> None:
    assert fingerprint("llm", {"a": 1, "b": 2}) == fingerprint("llm", {"b": 2, "a": 1})


def test_fingerprint_differs_for_different_payloads() -> None:
    assert fingerprint("llm", {"prompt": "甲"}) != fingerprint("llm", {"prompt": "乙"})
    assert fingerprint("llm", {"prompt": "x"}) != fingerprint("tool", {"prompt": "x"})


def test_fingerprint_is_prefixed_by_kind() -> None:
    assert fingerprint("tool", {}).startswith("tool:")


# ── 加载与校验 ────────────────────────────────────────────────────────────────

def test_replay_with_a_missing_file_loads_empty_instead_of_raising(tmp_path: Path) -> None:
    cassette = Cassette.load(tmp_path / "nope.json", REPLAY)

    assert cassette.entries == {}
    assert cassette.mode == REPLAY


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        Cassette(mode="whatever")


def test_version_mismatch_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    path.write_text(json.dumps({"version": 999, "entries": {}}), encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        Cassette.load(path, REPLAY)

    assert "版本不匹配" in str(excinfo.value)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "cassette.json"
    cassette = Cassette(path=path, mode=LIVE)
    cassette.put("llm:abc", {"kind": "llm", "request": {}, "response": "你好"})
    cassette.save()

    reloaded = Cassette.load(path, REPLAY)

    assert reloaded.entries == cassette.entries
    assert reloaded.get("llm:abc")["response"] == "你好"


# ── live 录制 ─────────────────────────────────────────────────────────────────

def test_live_mode_records_llm_and_tool_calls(tmp_path: Path) -> None:
    path = tmp_path / "recorded.json"
    agent, cassette = cassette_agent(path, mode=LIVE, planner=ScriptedPlanner(planner_script()))

    outcome = run(agent.run(blurred_context()))
    cassette.save()

    assert outcome.decision_engine == "llm"
    kinds = {entry["kind"] for entry in cassette.entries.values()}
    assert kinds == {"llm", "tool"}
    assert cassette.recorded == len(cassette.entries)
    assert cassette.stats()["mode"] == LIVE

    # 文件本身要是可读的 JSON，方便人工审阅
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == CASSETTE_VERSION
    assert raw["entries"]


# ── replay 回放 ───────────────────────────────────────────────────────────────

def test_replay_returns_recorded_responses_without_calling_the_model(tmp_path: Path) -> None:
    path = tmp_path / "recorded.json"
    recording_llm = ScriptedPlanner(planner_script())
    live_agent, cassette = cassette_agent(path, mode=LIVE, planner=recording_llm)
    run(live_agent.run(blurred_context()))
    cassette.save()
    calls_after_recording = recording_llm.calls

    class Exploding(ScriptedPlanner):
        async def complete(self, prompt: str, **_: Any) -> str:
            raise AssertionError("replay 模式绝不允许真的调用模型")

    replay_agent, _ = cassette_agent(path, mode=REPLAY, planner=Exploding(planner_script()))
    outcome = run(replay_agent.run(blurred_context()))

    assert recording_llm.calls == calls_after_recording
    assert outcome.decision_engine == "llm"
    assert outcome.answer == "根因在影像质量层。"


def test_replay_is_byte_identical_across_runs(tmp_path: Path) -> None:
    """同一输入连续跑两次，结果必须逐字节一致 —— 这是 CI 可用的前提。"""
    path = tmp_path / "recorded.json"
    live_agent, cassette = cassette_agent(path, mode=LIVE, planner=ScriptedPlanner(planner_script()))
    run(live_agent.run(blurred_context()))
    cassette.save()

    def once() -> str:
        agent, _ = cassette_agent(path, mode=REPLAY, planner=ScriptedPlanner(planner_script()))
        outcome = run(agent.run(blurred_context())).to_dict()
        # latency_ms 天然会变，比对时排除掉；其余必须完全一致。
        # 递归剔除而不是逐个位置 pop：耗时字段散落在 trace、trace[*].tool_events、
        # tools[*].avg_latency_ms 好几层。漏一层，那一层就会用毫秒级抖动
        # 随缘把测试翻红 —— 偶发失败比稳定失败更耗人，因为它看着像环境问题。
        drop_volatile(outcome)
        return json.dumps(outcome, ensure_ascii=False, sort_keys=True)

    assert once() == once()


# ── 关键契约：未命中必须失败，且不许联网 ──────────────────────────────────────

def test_replay_miss_raises_and_never_falls_back_to_the_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 cassette 时的行为，是这套机制全部价值所在。

    验证两件事：
    1. 抛 ``CassetteMissError``（测试会红，不会绿着骗人）；
    2. 真的没有出网 —— 把 ``urlopen`` 换成会炸的实现，它一次都没被调用。
    """
    def explode(*_: Any, **__: Any) -> Any:
        raise AssertionError("replay 模式下不允许发起任何网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    empty_cassette = tmp_path / "empty.json"
    # 内层故意放一个「真的会去打网络」的客户端：只要它被调用，测试就会炸
    real_client = HttpLLMClient(
        provider="openai",
        api_key="sk-not-a-real-key",
        model="gpt-4o-mini",
        base_url="http://127.0.0.1:1",
        timeout_s=1.0,
    )
    agent, _ = cassette_agent(empty_cassette, mode=REPLAY, planner=real_client)

    with pytest.raises(CassetteMissError) as excinfo:
        run(agent.run(blurred_context()))

    assert "没有录制" in str(excinfo.value)


def test_cassette_miss_is_not_swallowed_by_the_tool_layer(tmp_path: Path) -> None:
    """工具层的 ``except Exception`` 不能把「录制缺失」吃掉。

    这是 ``CassetteMissError`` 派生自 ``BaseException`` 的原因：
    否则 Agent 会把它当成「工具坏了」继续跑完，测试绿着骗人。
    """
    path = tmp_path / "only-llm.json"
    agent, cassette = cassette_agent(path, mode=LIVE, planner=ScriptedPlanner(planner_script()))
    run(agent.run(blurred_context()))

    # 保留 LLM 的录制，但把工具的录制全部删掉，制造「工具未命中」
    cassette.save()
    loaded = Cassette.load(path, REPLAY)
    loaded.entries = {
        key: value for key, value in loaded.entries.items() if value["kind"] == "llm"
    }
    loaded.save()

    replay_agent, _ = cassette_agent(path, mode=REPLAY, planner=ScriptedPlanner(planner_script()))

    with pytest.raises(CassetteMissError):
        run(replay_agent.run(blurred_context()))


def test_replay_available_is_true_so_misses_are_loud() -> None:
    """replay 下 ``available`` 恒为 True：录制说了算。"""
    client = CassetteLLMClient(inner=NullLLMClient(), cassette=Cassette(mode=REPLAY))

    assert client.available is True

    live = CassetteLLMClient(inner=NullLLMClient(), cassette=Cassette(mode=LIVE))
    assert live.available is False


def test_cassette_client_name_marks_the_mode() -> None:
    client = CassetteLLMClient(inner=NullLLMClient(), cassette=Cassette(mode=REPLAY))

    assert "cassette:replay" in client.name


# ── 工具包装 ──────────────────────────────────────────────────────────────────

def sample_tool(calls: List[str]) -> Tool:
    def handler(params: Dict[str, Any], context: Any) -> List[str]:
        calls.append(str(params.get("query")))
        return [f"hit:{params.get('query')}"]

    return Tool(
        name="sample",
        description="样例",
        handler=handler,
        schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
    )


def test_wrapped_tool_records_and_replays(tmp_path: Path) -> None:
    calls: List[str] = []
    recorded = wrap_tool(sample_tool(calls), Cassette(mode=LIVE))
    assert recorded.handler({"query": "模糊"}, None) == ["hit:模糊"]

    cassette = Cassette.load(tmp_path / "t.json", REPLAY)
    cassette.entries = {
        fingerprint("tool", {"tool": "sample", "params": {"query": "模糊"}}): {
            "kind": "tool",
            "request": {},
            "response": ["hit:模糊"],
        }
    }
    replayed = wrap_tool(sample_tool([]), cassette)

    assert replayed.handler({"query": "模糊"}, None) == ["hit:模糊"]
    assert calls == ["模糊"]


def test_wrapped_tool_miss_raises(tmp_path: Path) -> None:
    tool = wrap_tool(sample_tool([]), Cassette(mode=REPLAY))

    with pytest.raises(CassetteMissError):
        tool.handler({"query": "没有录过"}, None)


def test_wrapping_preserves_tool_metadata() -> None:
    original = sample_tool([])
    wrapped = wrap_tool(original, Cassette(mode=LIVE))

    assert wrapped.name == original.name
    assert wrapped.schema == original.schema
    assert wrapped.timeout_s == original.timeout_s
