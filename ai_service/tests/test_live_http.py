"""协议级验证：真实 HTTP、鉴权头、响应解析、usage 抽取、cassette 录制回放。

为什么需要这一层
----------------
进程内假 LLM 能测 Agent 的逻辑，但有一整段代码永远测不到：
``HttpLLMClient`` 的请求构造、鉴权头、响应解析、错误映射、usage 抽取。
这段代码只在连真实 provider 时才跑，而本机当时没有 key ——
于是它成了「最容易被追问、又恰恰没验证」的一环。

这里用本地协议靶子（``ai_service.devtools.mock_llm``）把它补上：
不联网、不花钱、无 key，但请求真的走了一遍 HTTP。

边界要说清楚
------------
这些用例证明的是**协议与传输正确**，不是**模型答得好**。
后者只能用真实模型验证，见 ``ai_service/README.md`` 的实测记录。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, Optional

import pytest

from ai_service.agent import build_agent
from ai_service.agentkit import blurred_context, run
from ai_service.cassette import (
    LIVE,
    REPLAY,
    Cassette,
    CassetteLLMClient,
    CassetteMissError,
    record_cassette,
)
from ai_service.devtools.mock_llm import (
    DEFAULT_API_KEY,
    MockLLMConfig,
    MockLLMServer,
)
from ai_service.llm import USAGE_PROVIDER, LLMUnavailableError, build_llm_client, take_usage
from ai_service.tools import ContextRecordSource


@pytest.fixture(autouse=True)
def bypass_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 urllib 不要代理回环地址。

    开发机 shell 里常见 ``http_proxy``，它会把 127.0.0.1 也一起代理掉，
    报出来的错还完全指不到代理头上。靶子是本地的，必须绕过。
    """
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def server() -> Iterator[MockLLMServer]:
    """固定 token 数，方便断言精确值。"""
    with MockLLMServer(
        MockLLMConfig(fixed_prompt_tokens=120, fixed_completion_tokens=30)
    ) as started:
        yield started


class ExplodingLLM:
    """调用即炸：用来证明 replay 真的没有碰网络。"""

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:exploding"

    async def complete(self, prompt: str, **_: Any) -> str:
        raise AssertionError("replay 模式下不应该调用内层客户端")


def _header(headers: Dict[str, str], name: str) -> Optional[str]:
    """按名字取请求头，**忽略大小写**。

    ``urllib.request.Request.add_header`` 会把名字 ``capitalize()``，
    所以发出去的其实是 ``X-api-key`` 而不是 ``x-api-key``。
    这里若用精确匹配，就会得出「key 没发出去」的错误结论 ——
    而实际上 header 名的大小写在 HTTP 里本来就不敏感。
    """
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _strip_variable_fields(outcome: Dict[str, Any]) -> str:
    """去掉天然会变的字段，其余逐字比对。

    去掉两类：
    * ``latency_ms`` —— 跑两次必然不同；
    * ``engine.llm`` —— 它如实报告「当前用的是哪个客户端」，
      live 是 ``cassette:live:llm:openai:...``，replay 是
      ``cassette:replay:llm:exploding``，本来就该不同。
    """
    clone = json.loads(json.dumps(outcome, ensure_ascii=False, default=str))
    clone.pop("latency_ms", None)
    (clone.get("engine") or {}).pop("llm", None)
    for entry in clone.get("trace") or []:
        entry.pop("latency_ms", None)
    for stats in (clone.get("tools") or {}).values():
        stats.pop("avg_latency_ms", None)
    return json.dumps(clone, ensure_ascii=False, sort_keys=True)


def _agent_outcome(llm: Any) -> Dict[str, Any]:
    context = blurred_context()
    agent = build_agent(
        llm=llm,
        records=ContextRecordSource(
            request_id=context.request_id, record=context.to_record_fields()
        ),
    )
    return run(agent.run(context)).to_dict()


# ── 两种协议各跑一遍 ──────────────────────────────────────────────────────────

def test_openai_compatible_round_trip_reports_real_usage(server: MockLLMServer) -> None:
    client = build_llm_client(server.openai_env())

    assert client.available
    text = run(client.complete("你好，请回一句话"))
    usage = take_usage(client)

    assert text
    assert server.requests[-1]["path"].endswith("/v1/chat/completions")
    assert usage is not None
    assert usage.source == USAGE_PROVIDER
    assert (usage.prompt_tokens, usage.completion_tokens) == (120, 30)
    assert usage.total == 150


def test_anthropic_round_trip_reads_its_own_usage_field_names(
    server: MockLLMServer,
) -> None:
    """Anthropic 用的是 input_tokens / output_tokens —— 字段名搞错会静默变 0。"""
    client = build_llm_client(server.anthropic_env())

    run(client.complete("你好"))
    usage = take_usage(client)

    assert server.requests[-1]["path"].endswith("/v1/messages")
    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens) == (120, 30)


def test_credentials_are_actually_put_on_the_wire(server: MockLLMServer) -> None:
    client = build_llm_client(server.openai_env(api_key="secret-abc"))

    run(client.complete("ping"))

    headers = server.requests[-1]["headers"]
    assert _header(headers, "Authorization") == "Bearer secret-abc"
    assert server.auth_failures == 0


def test_anthropic_uses_the_api_key_header(server: MockLLMServer) -> None:
    client = build_llm_client(server.anthropic_env(api_key="secret-xyz"))

    run(client.complete("ping"))

    headers = server.requests[-1]["headers"]
    assert _header(headers, "x-api-key") == "secret-xyz"
    assert _header(headers, "anthropic-version")


def test_the_target_really_checks_auth(server: MockLLMServer) -> None:
    """靶子自己必须会拒绝匿名请求，否则上面那条「带了 key」是假证据。"""
    request = urllib.request.Request(
        f"{server.base_url_openai}/chat/completions",
        data=b'{"messages":[]}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)

    assert excinfo.value.code == 401
    assert server.auth_failures == 1


# ── 错误路径 ──────────────────────────────────────────────────────────────────

def test_http_500_becomes_a_catchable_unavailable_error() -> None:
    with MockLLMServer(MockLLMConfig(fail_first_n=1)) as failing:
        client = build_llm_client(failing.openai_env())

        with pytest.raises(LLMUnavailableError) as excinfo:
            run(client.complete("ping"))

    assert "HTTP 500" in str(excinfo.value)
    assert getattr(client, "last_error", "") .startswith("HTTP 500")


def test_a_provider_without_usage_yields_none_so_the_caller_can_estimate() -> None:
    """缺 usage 必须是 ``None`` 而不是 0 —— 0 会被当成「这次不花钱」。"""
    with MockLLMServer(MockLLMConfig(omit_usage=True)) as silent:
        client = build_llm_client(silent.openai_env())

        assert run(client.complete("ping"))
        assert take_usage(client) is None


# ── Agent 端到端（真的走 HTTP）───────────────────────────────────────────────

def test_agent_runs_end_to_end_over_http(server: MockLLMServer) -> None:
    client = build_llm_client(server.openai_env())

    outcome = _agent_outcome(client)
    usage = outcome["token_usage"]

    assert outcome["engine"]["decision"] == "llm"
    assert outcome["degraded"] is False
    assert outcome["stop_reason"] == "finished"
    assert [e["tool"] for e in outcome["trace"] if e.get("executed")] == ["search_knowledge"]
    assert outcome["prompt_versions"]["used"]["agent_decide"] == "agent_decide@v1"

    assert usage["source"] == USAGE_PROVIDER
    assert usage["llm_calls"] == 2
    assert usage["prompt_tokens"] == 240  # 两次决策 × 120
    assert usage["completion_tokens"] == 60
    assert usage["total"] == 300
    assert outcome["budget_used"]["tokens"] == 300


# ── cassette：录制在真实 HTTP 上，回放在完全离线 ─────────────────────────────

def test_a_cassette_recorded_over_http_replays_without_any_network(
    tmp_path: Any,
    server: MockLLMServer,
) -> None:
    path = tmp_path / "cassette.json"
    live_client, cassette = record_cassette(
        path, build_llm_client(server.openai_env()), mode=LIVE
    )

    first = _agent_outcome(live_client)
    cassette.save()

    assert cassette.recorded == 2
    assert path.is_file()

    # 回放：内层换成「调用即炸」的客户端，一旦真的联网就会立刻失败
    replay_cassette = Cassette.load(path, REPLAY)
    replay_client = CassetteLLMClient(inner=ExplodingLLM(), cassette=replay_cassette)

    second = _agent_outcome(replay_client)

    assert replay_cassette.hits == 2
    assert replay_cassette.misses == 0
    # 轨迹与成本逐字一致 —— 包括用量，它跟着录制一起回来了
    assert _strip_variable_fields(first) == _strip_variable_fields(second)
    assert second["token_usage"] == first["token_usage"]
    assert second["token_usage"]["source"] == USAGE_PROVIDER


def test_replay_without_a_recording_fails_loudly_instead_of_going_online(
    tmp_path: Any,
) -> None:
    cassette = Cassette.load(tmp_path / "does-not-exist.json", REPLAY)
    client = CassetteLLMClient(inner=ExplodingLLM(), cassette=cassette)

    with pytest.raises(CassetteMissError):
        run(client.complete("这条 prompt 从未录制过"))


# ── CLI 的两条路径 ────────────────────────────────────────────────────────────

def test_agent_cli_stays_offline_even_when_a_key_is_configured(
    server: MockLLMServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """硬约束：``--agent`` 默认离线，**哪怕 shell 里配了 key**。

    「跑一次冒烟」不该有意外花钱的可能。这条如果破了，测试必须红。
    """
    for name, value in server.openai_env().items():
        monkeypatch.setenv(name, value)

    from ai_service import __main__ as cli

    code = cli.main(["--agent", "--indent", "0"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["engine"]["decision"] == "rule"
    assert payload["token_usage"]["source"] == "none"
    assert server.request_count == 0  # 一次请求都没发出去


def test_agent_live_cli_uses_the_configured_model(
    server: MockLLMServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in server.openai_env().items():
        monkeypatch.setenv(name, value)

    from ai_service import __main__ as cli

    code = cli.main(["--agent", "--live", "--indent", "0"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["engine"]["decision"] == "llm"
    assert payload["token_usage"]["source"] == USAGE_PROVIDER
    assert server.request_count >= 2


def test_agent_live_cli_without_a_key_explains_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ai_service import __main__ as cli

    code = cli.main(["--agent", "--live"])
    captured = capsys.readouterr()

    assert code == 2
    assert "没有可用的模型" in captured.err
    assert "LLM_API_KEY" in captured.err


def test_live_explainer_also_reports_usage(
    server: MockLLMServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--live`` 跑的是解释链路，它的用量也要能报出来。"""
    for name, value in server.openai_env().items():
        monkeypatch.setenv(name, value)

    from ai_service import __main__ as cli

    code = cli.main(["--live", "--indent", "0"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["engine"]["generation"] == "llm"
    usage = payload["token_usage"]
    assert usage["source"] == USAGE_PROVIDER
    assert usage["prompt_tokens"] > 0
    # 解释链路会调改写 + 重排 + 生成，不止一次
    assert usage["total_tokens"] > 120


def test_the_target_is_reachable_with_the_default_key(server: MockLLMServer) -> None:
    """兜底自检：靶子必须能用默认 key 打通，否则上面所有用例都是空跑。"""
    client = build_llm_client(server.openai_env(api_key=DEFAULT_API_KEY))

    assert run(client.complete("ping"))
    assert server.request_count == 1
