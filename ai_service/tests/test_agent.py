"""审核 Agent 的行为测试。

对应 P1.2 的七条验收标准，每条一个（或一组）测试：

1. 有明确原因码 → 自主选 ``search_knowledge`` 并给出答复
2. 信息不足 → 调 ``escalate_to_human``
3. 非法工具名被拒，trace 有拒绝记录，任务不崩溃
4. 参数不符合 schema 被拒
5. ``max_steps=2`` 时两步收敛且 ``truncated=True``
6. LLM 不可用时走确定性序列，输出结构与 LLM 路径一致
7. 工具超时 / 报错时降级而非抛异常

外加：越权读取、重复调用、白名单外工具不进 prompt 清单。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import pytest

from ai_service.agent import (
    AgentBudget,
    ReviewAgent,
    build_agent,
    estimate_tokens,
    run_agent_for_context,
)
from ai_service.explain import ReviewContext
from ai_service.llm import LLMUnavailableError, NullLLMClient
from ai_service.tools import (
    ContextRecordSource,
    ESCALATE_TO_HUMAN,
    GET_REVIEW_RECORD,
    RECOMPUTE_QUALITY,
    SEARCH_KNOWLEDGE,
    TOOL_WHITELIST,
    tool_catalog_text,
)
from ai_service.tool_manager import Tool


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── 替身 ──────────────────────────────────────────────────────────────────────

class ScriptedPlanner:
    """按脚本给出决策的假 planner。脚本用尽后重复最后一条。"""

    def __init__(self, decisions: List[Dict[str, Any]]) -> None:
        if not decisions:
            raise ValueError("脚本不能为空")
        self.decisions = decisions
        self.calls = 0
        self.prompts: List[str] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:scripted:planner"

    async def complete(self, prompt: str, **_: Any) -> str:
        self.prompts.append(prompt)
        decision = self.decisions[min(self.calls, len(self.decisions) - 1)]
        self.calls += 1
        return json.dumps(decision, ensure_ascii=False)


class BrokenPlanner:
    """决策阶段就炸的 planner。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:broken"

    async def complete(self, prompt: str, **_: Any) -> str:
        raise self.exc


def decision(action: str, answer: str = "", **params: Any) -> Dict[str, Any]:
    """构造一条决策。

    ``answer`` 是决策对象的顶层字段，**不是**工具参数 —— 早期把它混进
    ``params`` 里，导致 finish 决策带不回答复，白排查了一轮。
    """
    return {
        "thought": f"准备调用 {action}",
        "action": action,
        "params": params,
        "answer": answer,
    }


def search_step(tool: str = SEARCH_KNOWLEDGE, **params: Any) -> Dict[str, Any]:
    return decision(tool, **params)


def make_agent(
    planner: Any = None,
    *,
    context: Optional[ReviewContext] = None,
    budget: Optional[AgentBudget] = None,
    records: Any = None,
) -> ReviewAgent:
    ctx = context or blurred_context()
    return build_agent(
        llm=planner if planner is not None else NullLLMClient(),
        budget=budget,
        records=records
        or ContextRecordSource(request_id=ctx.request_id, record=ctx.to_record_fields()),
    )


def blurred_context(**overrides: Any) -> ReviewContext:
    payload: Dict[str, Any] = {
        "request_id": "req-001",
        "doc_type": "bank_card",
        "review_result": "review",
        "quality_result": "review",
        "quality_reasons": ["image_blur"],
        "review_reasons": ["missing_valid_date", "image_blur"],
        "fields": {"card_number": "6222******7890", "name": "张*"},
        "question": "这张卡为什么需要人工复核？",
    }
    payload.update(overrides)
    return ReviewContext.from_payload(payload)


def tools_used(outcome: Dict[str, Any]) -> List[str]:
    """实际执行过的工具序列。被拒绝的调用（``executed=False``）不算执行。"""
    return [entry["tool"] for entry in outcome["trace"] if entry.get("executed")]


# ── 1. 自主选工具完成任务 ─────────────────────────────────────────────────────

def test_agent_picks_search_knowledge_and_answers() -> None:
    planner = ScriptedPlanner(
        [
            search_step(query="image_blur 是什么意思", top_k=5),
            decision("finish", answer="根因在影像质量层，建议让用户重拍。"),
        ]
    )

    outcome = run(make_agent(planner).run(blurred_context()))

    assert tools_used(outcome.to_dict()) == [SEARCH_KNOWLEDGE]
    assert outcome.stop_reason == "finished"
    assert outcome.decision_engine == "llm"
    assert outcome.degraded is False
    assert outcome.answer == "根因在影像质量层，建议让用户重拍。"
    # 事实层与模型措辞分离：原因码释义仍来自语料
    assert {item["code"] for item in outcome.reason_details} == {
        "missing_valid_date",
        "image_blur",
    }


def test_agent_can_chain_multiple_tools() -> None:
    planner = ScriptedPlanner(
        [
            decision(GET_REVIEW_RECORD, request_id="req-001"),
            search_step(query="missing_valid_date 释义"),
            decision(RECOMPUTE_QUALITY),
            decision("finish", answer="综合解释。"),
        ]
    )

    outcome = run(make_agent(planner).run(blurred_context()))

    assert tools_used(outcome.to_dict()) == [
        GET_REVIEW_RECORD,
        SEARCH_KNOWLEDGE,
        RECOMPUTE_QUALITY,
    ]
    assert outcome.quality_audit is not None
    assert outcome.quality_audit["mode"] == "flags"


def test_agent_falls_back_to_template_when_finish_has_no_answer() -> None:
    # 先正常查到证据，再给一个没带 answer 的 finish —— 隔离出「答复为空」这一种情况，
    # 否则「无证据 → 转人工」规则会先生效，测的就不是这件事了
    planner = ScriptedPlanner([search_step(query="模糊"), decision("finish")])

    outcome = run(make_agent(planner).run(blurred_context()))

    assert outcome.stop_reason == "finished"
    # 模型没给答复也不能空着 —— 事实层本来就是齐的
    assert "待人工复核" in outcome.answer


# ── 2. 证据不足时举手找人 ─────────────────────────────────────────────────────

def test_agent_escalates_when_the_record_has_no_reason_codes() -> None:
    context = blurred_context(
        review_result="review",
        quality_result=None,
        quality_reasons=[],
        review_reasons=[],
    )

    outcome = run(make_agent().run(context))

    assert outcome.stop_reason == "escalated"
    assert outcome.escalation is not None
    assert outcome.escalation["escalated"] is True
    assert "没有原因码" in outcome.escalation["reason"]
    assert tools_used(outcome.to_dict())[-1] == ESCALATE_TO_HUMAN
    # 举手表态必须无副作用 —— 不能偷偷改判
    assert outcome.escalation["side_effects"] == []


def test_agent_escalates_on_unknown_reason_codes() -> None:
    context = blurred_context(review_reasons=["totally_new_code"], quality_reasons=[])

    outcome = run(make_agent().run(context))

    assert outcome.stop_reason == "escalated"
    assert outcome.unknown_reason_codes == ["totally_new_code"]
    assert "未收录" in outcome.escalation["reason"]
    assert outcome.escalation["missing_evidence"]


def test_escalation_is_reflected_in_the_answer() -> None:
    context = blurred_context(review_result="reject", quality_reasons=[], review_reasons=[])

    outcome = run(make_agent().run(context))

    assert "转人工复核" in outcome.answer


# ── 3. 非法工具名被拒绝 ───────────────────────────────────────────────────────

def test_non_whitelisted_tool_is_rejected_and_recorded() -> None:
    planner = ScriptedPlanner(
        [
            decision("delete_review_record", request_id="req-001"),
            search_step(query="模糊"),
            decision("finish", answer="改用已知信息作答。"),
        ]
    )

    outcome = run(make_agent(planner).run(blurred_context()))
    payload = outcome.to_dict()

    rejected = [entry for entry in payload["trace"] if entry.get("rejected") == "not_whitelisted"]
    assert len(rejected) == 1
    assert rejected[0]["tool"] == "delete_review_record"
    assert "白名单" in rejected[0]["error"]
    # 任务不崩溃，仍能收敛
    assert outcome.stop_reason == "finished"
    assert "delete_review_record" not in tools_used(payload)


def test_repeated_illegal_calls_converge_instead_of_burning_budget() -> None:
    planner = ScriptedPlanner([decision("rm_rf_everything")])

    outcome = run(make_agent(planner).run(blurred_context()))

    assert outcome.stop_reason == "too_many_rejections"
    assert outcome.truncated is True
    assert outcome.budget_used["rejections"] >= 3
    # 一次工具都没成功执行
    assert outcome.budget_used["tool_calls"] == 0


def test_whitelist_out_tools_never_reach_the_prompt() -> None:
    catalog = tool_catalog_text()

    for name in TOOL_WHITELIST:
        assert name in catalog
    assert "delete_review_record" not in catalog
    assert "shell" not in catalog


# ── 4. 参数不符合 schema 被拒 ─────────────────────────────────────────────────

def test_missing_required_param_is_rejected_before_the_tool_runs() -> None:
    planner = ScriptedPlanner(
        [
            {"thought": "忘了传 query", "action": SEARCH_KNOWLEDGE, "params": {}},
            search_step(query="模糊"),
            decision("finish", answer="补上了。"),
        ]
    )

    outcome = run(make_agent(planner).run(blurred_context()))
    payload = outcome.to_dict()

    rejected = [entry for entry in payload["trace"] if entry.get("rejected") == "invalid_params"]
    assert len(rejected) == 1
    assert "query" in rejected[0]["error"]
    # 关键断言：被拒的那一次没有真的执行 —— 参数错了就不该碰工具
    assert rejected[0]["executed"] is False
    # 后面补对参数的那一次正常执行了，说明拒绝不是把工具封掉
    assert tools_used(payload) == [SEARCH_KNOWLEDGE]


def test_wrong_param_type_is_rejected() -> None:
    planner = ScriptedPlanner(
        [search_step(query="模糊", top_k="五个"), decision("finish", answer="好。")]
    )

    outcome = run(make_agent(planner).run(blurred_context()))
    payload = outcome.to_dict()

    rejected = [entry for entry in payload["trace"] if entry.get("rejected") == "invalid_params"]
    assert rejected
    assert "top_k" in rejected[0]["error"]


# ── 5. 步数预算 ───────────────────────────────────────────────────────────────

def test_max_steps_truncates_the_run() -> None:
    # 模型想一直查下去；预算只给 2 步
    planner = ScriptedPlanner([search_step(query="继续查")])

    outcome = run(
        make_agent(planner, budget=AgentBudget(max_steps=2)).run(blurred_context())
    )

    assert outcome.budget_used["steps"] == 2
    assert outcome.truncated is True
    assert outcome.stop_reason == "max_steps"
    # 超预算照常返回已有结果，不抛异常
    assert outcome.answer
    assert len(outcome.citations) > 0


def test_token_budget_stops_the_loop_early() -> None:
    planner = ScriptedPlanner([search_step(query="查" * 50)])

    outcome = run(
        make_agent(planner, budget=AgentBudget(max_steps=10, max_tokens=10)).run(
            blurred_context()
        )
    )

    assert outcome.stop_reason == "max_tokens"
    assert outcome.truncated is True


def test_tool_call_budget_stops_the_loop_early() -> None:
    planner = ScriptedPlanner([search_step(query="换着花样查")])

    outcome = run(
        make_agent(planner, budget=AgentBudget(max_steps=10, max_tool_calls=2)).run(
            blurred_context()
        )
    )

    assert outcome.stop_reason == "max_tool_calls"
    assert outcome.budget_used["tool_calls"] == 2


def test_identical_repeated_call_is_treated_as_spinning() -> None:
    planner = ScriptedPlanner([search_step(query="同一个问题", top_k=5)])

    outcome = run(make_agent(planner).run(blurred_context()))

    assert outcome.stop_reason == "repeat_call"
    rejected = [entry for entry in outcome.trace if entry.get("rejected") == "repeat_call"]
    assert rejected
    # 允许重复两次，第三次才判定打转
    assert outcome.budget_used["tool_calls"] == 2


# ── 6. 降级路径结构一致 ───────────────────────────────────────────────────────

def test_degraded_path_uses_a_deterministic_tool_sequence() -> None:
    outcome = run(make_agent(NullLLMClient()).run(blurred_context()))

    assert outcome.decision_engine == "rule"
    assert outcome.degraded is True
    assert outcome.stop_reason == "finished"
    assert tools_used(outcome.to_dict()) == [
        GET_REVIEW_RECORD,
        SEARCH_KNOWLEDGE,
        RECOMPUTE_QUALITY,
    ]
    # 降级路径不该声称用了 prompt
    assert outcome.prompt_versions["used"] == {}


def test_both_paths_return_the_same_shape() -> None:
    """两条路径的字段集合必须一致，否则前端要写两套渲染。"""
    llm_outcome = run(
        make_agent(
            ScriptedPlanner([search_step(query="模糊"), decision("finish", answer="好的。")])
        ).run(blurred_context())
    ).to_dict()
    rule_outcome = run(make_agent(NullLLMClient()).run(blurred_context())).to_dict()

    assert set(llm_outcome) == set(rule_outcome)
    assert set(llm_outcome["engine"]) == set(rule_outcome["engine"])
    assert set(llm_outcome["budget"]) == set(rule_outcome["budget"])


def test_llm_path_reports_the_decision_prompt_version() -> None:
    planner = ScriptedPlanner([decision("finish", answer="够了。")])

    outcome = run(make_agent(planner).run(blurred_context()))

    assert outcome.prompt_versions["used"]["agent_decide"] == "agent_decide@v1"


def test_unusable_decision_output_degrades_to_the_deterministic_path() -> None:
    class ReturnsProse(ScriptedPlanner):
        async def complete(self, prompt: str, **_: Any) -> str:
            return "我觉得应该重拍。"

    outcome = run(make_agent(ReturnsProse([decision("finish")])).run(blurred_context()))

    assert outcome.decision_engine == "rule"
    assert outcome.truncated is False
    failed = [entry for entry in outcome.trace if entry.get("step") == "decision_failed"]
    assert failed and failed[0]["reason"] == "unparseable"


def test_llm_unavailable_midway_switches_to_the_deterministic_path() -> None:
    outcome = run(
        make_agent(BrokenPlanner(LLMUnavailableError("上游 503"))).run(blurred_context())
    )

    assert outcome.decision_engine == "rule"
    assert outcome.answer
    failed = [entry for entry in outcome.trace if entry.get("step") == "decision_failed"]
    assert failed and failed[0]["reason"] == "llm_unavailable"


# ── 7. 工具故障 → 降级不抛异常 ────────────────────────────────────────────────

def _override_tool(agent: ReviewAgent, name: str, handler: Any, **kwargs: Any) -> None:
    """把已注册的工具换成会出故障的实现（故障注入）。"""
    agent.tools.register(
        Tool(
            name=name,
            description="故障注入替身",
            handler=handler,
            schema={"type": "object", "properties": {"query": {"type": "string"}}},
            timeout_s=kwargs.pop("timeout_s", 0.2),
            **kwargs,
        )
    )


def test_tool_error_degrades_instead_of_raising() -> None:
    def boom(params: Dict[str, Any], context: Any) -> Any:
        raise RuntimeError("知识库连接被拒")

    planner = ScriptedPlanner([search_step(query="模糊"), decision("finish", answer="降级作答。")])
    agent = make_agent(planner)
    _override_tool(agent, SEARCH_KNOWLEDGE, boom)

    outcome = run(agent.run(blurred_context()))

    step = next(entry for entry in outcome.trace if entry.get("tool") == SEARCH_KNOWLEDGE)
    assert step["ok"] is False
    assert "连接被拒" in step["error"]
    # 工具挂了不影响事实层
    assert {item["code"] for item in outcome.reason_details} == {
        "missing_valid_date",
        "image_blur",
    }
    # 检索没拿到证据 → 「证据不足」规则接管，如实转人工而不是硬编一个答复
    assert outcome.stop_reason == "escalated"
    assert outcome.escalation is not None


def test_tool_timeout_degrades_instead_of_raising() -> None:
    def slow(params: Dict[str, Any], context: Any) -> Any:
        time.sleep(0.5)
        return []

    planner = ScriptedPlanner([search_step(query="模糊"), decision("finish", answer="降级作答。")])
    agent = make_agent(planner)
    _override_tool(agent, SEARCH_KNOWLEDGE, slow, timeout_s=0.1)

    outcome = run(agent.run(blurred_context()))

    step = next(entry for entry in outcome.trace if entry.get("tool") == SEARCH_KNOWLEDGE)
    assert step["ok"] is False
    assert any(event["step"] == "tool_timeout" for event in step["tool_events"])


def test_tool_failure_on_the_deterministic_path_still_produces_an_answer() -> None:
    def boom(params: Dict[str, Any], context: Any) -> Any:
        raise RuntimeError("挂了")

    agent = make_agent(NullLLMClient())
    _override_tool(agent, SEARCH_KNOWLEDGE, boom)
    _override_tool(agent, GET_REVIEW_RECORD, boom)

    outcome = run(agent.run(blurred_context()))

    assert outcome.answer
    assert outcome.reason_details


# ── 越权 / 数据边界 ───────────────────────────────────────────────────────────

def test_agent_cannot_read_another_request_record() -> None:
    planner = ScriptedPlanner(
        [decision(GET_REVIEW_RECORD, request_id="someone-elses-id"), decision("finish")]
    )

    outcome = run(make_agent(planner).run(blurred_context()))
    step = next(entry for entry in outcome.trace if entry.get("tool") == GET_REVIEW_RECORD)

    assert step["ok"] is True  # 工具本身正常返回
    assert "记录不存在" in step["observation"]


def test_record_source_only_exposes_whitelisted_fields() -> None:
    source = ContextRecordSource(
        request_id="req-001",
        record={**blurred_context().to_record_fields(), "password_hash": "should-not-leak"},
    )

    record = source.get("req-001")

    assert record is not None
    assert "password_hash" not in record


# ── trace 结构 ────────────────────────────────────────────────────────────────

def test_every_executed_step_carries_the_required_trace_fields() -> None:
    planner = ScriptedPlanner([search_step(query="模糊"), decision("finish", answer="好。")])

    outcome = run(make_agent(planner).run(blurred_context()))
    steps = [entry for entry in outcome.trace if entry.get("executed")]

    assert steps
    for entry in steps:
        for key in (
            "step",
            "thought",
            "tool",
            "params",
            "ok",
            "observation",
            "latency_ms",
            "prompt",
        ):
            assert key in entry, key
        assert 0.0 <= entry["latency_ms"]


def test_rejected_calls_are_marked_as_not_executed() -> None:
    """拒绝与失败要能在 trace 里区分开 —— 审计上它们是两件事。"""
    planner = ScriptedPlanner([decision("sudo_rm_rf"), decision("finish", answer="好。")])

    outcome = run(make_agent(planner).run(blurred_context()))
    rejected = [entry for entry in outcome.trace if entry.get("rejected")]

    assert rejected
    for entry in rejected:
        assert entry["executed"] is False
        assert "ok" not in entry


def test_trace_can_be_serialised_for_the_platform() -> None:
    outcome = run(make_agent(NullLLMClient()).run(blurred_context()))

    # 平台侧要把它塞进 JSON 响应，不能有不可序列化的对象
    json.dumps(outcome.to_dict(), ensure_ascii=False)


def test_tool_stats_are_reported() -> None:
    outcome = run(make_agent(NullLLMClient()).run(blurred_context()))

    assert SEARCH_KNOWLEDGE in outcome.tools
    assert outcome.tools[SEARCH_KNOWLEDGE]["success"] >= 1


# ── 其它 ──────────────────────────────────────────────────────────────────────

def test_run_agent_for_context_returns_a_plain_dict() -> None:
    payload = run(run_agent_for_context(blurred_context()))

    assert isinstance(payload, dict)
    assert payload["request_id"] == "req-001"
    # 同时给出 explanation 别名，便于前端复用 P0 面板
    assert payload["explanation"] == payload["answer"]


def test_estimate_tokens_grows_with_length() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("a" * 40) == 10
    assert estimate_tokens("中" * 100) > estimate_tokens("中" * 10)


def test_agent_does_not_escalate_for_a_clean_pass() -> None:
    """显式 pass 且无原因码 = 自解释，不该无谓地转人工。"""
    context = blurred_context(
        review_result="pass",
        quality_result="pass",
        quality_reasons=[],
        review_reasons=[],
    )

    outcome = run(make_agent().run(context))

    assert outcome.stop_reason == "finished"
    assert outcome.escalation is None
    assert ESCALATE_TO_HUMAN not in tools_used(outcome.to_dict())


def test_agent_escalates_when_the_conclusion_is_missing() -> None:
    """结论为空又没有任何原因码 —— 来路不明，不能替审核员编解释。"""
    context = blurred_context(
        review_result="",
        quality_result=None,
        quality_reasons=[],
        review_reasons=[],
    )

    outcome = run(make_agent().run(context))

    assert outcome.stop_reason == "escalated"
    assert outcome.escalation is not None


def test_question_defaults_when_not_provided() -> None:
    context = blurred_context(question="")

    outcome = run(make_agent().run(context))

    assert outcome.question
    assert outcome.question == context.effective_question


@pytest.mark.parametrize("budget_field", ["max_steps", "max_tokens", "max_tool_calls"])
def test_budget_is_reported_in_the_result(budget_field: str) -> None:
    outcome = run(make_agent(NullLLMClient()).run(blurred_context()))

    assert budget_field in outcome.budget
