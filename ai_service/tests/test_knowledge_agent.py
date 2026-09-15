"""客服 Agent 的主流程：工具白名单、预算、降级、去偏、接口。

三个安全维度（接地 / 越界 / 合规）各有独立文件，这里只测**机制**：
循环怎么走、闸门在哪、降级之后还剩什么。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_service.agentkit import ScriptedPlanner, decision, finish_step, run
from ai_service.knowledge import policy
from ai_service.knowledge.agent import (
    KnowledgeBudget,
    build_knowledge_agent,
    run_knowledge_ask,
)
from ai_service.knowledge.api import create_knowledge_router
from ai_service.knowledge.tools import (
    HANDOFF_TO_HUMAN,
    KNOWLEDGE_TOOL_WHITELIST,
    LOOKUP_PRODUCT,
    SEARCH_FAQ,
)
from ai_service.llm import NullLLMClient

MATERIALS_QUESTION = "办理二类账户需要哪些材料？"
OFF_TOPIC_QUESTION = "你们家的私人飞机怎么买？"


def ask(question: str, *, planner: Any = None, budget: Any = None) -> Dict[str, Any]:
    agent = build_knowledge_agent(llm=planner, budget=budget)
    return run(agent.ask(question)).to_dict()


def tools_used(outcome: Dict[str, Any]) -> List[str]:
    return [
        entry["tool"]
        for entry in outcome["trace"]
        if isinstance(entry, dict) and entry.get("executed")
    ]


# ── 白名单是硬的 ──────────────────────────────────────────────────────────────

def test_only_read_only_tools_are_whitelisted() -> None:
    assert KNOWLEDGE_TOOL_WHITELIST == {
        "search_faq",
        "search_policy",
        "lookup_product",
        "handoff_to_human",
    }


def test_a_tool_outside_the_whitelist_is_rejected_and_recorded() -> None:
    planner = ScriptedPlanner(
        [
            decision("delete_customer_record", customer_id="c-1"),
            finish_step("改用已有信息作答。"),
        ]
    )

    outcome = ask(MATERIALS_QUESTION, planner=planner)
    rejected = [
        entry for entry in outcome["trace"] if entry.get("rejected") == "not_whitelisted"
    ]

    assert len(rejected) == 1
    assert rejected[0]["tool"] == "delete_customer_record"
    assert "白名单" in rejected[0]["error"]
    assert "delete_customer_record" not in tools_used(outcome)


def test_a_missing_required_param_is_rejected_before_the_tool_runs() -> None:
    planner = ScriptedPlanner(
        [
            {"thought": "忘了传 query", "action": SEARCH_FAQ, "params": {}},
            finish_step("补上了。"),
        ]
    )

    outcome = ask(MATERIALS_QUESTION, planner=planner)
    rejected = [
        entry for entry in outcome["trace"] if entry.get("rejected") == "invalid_params"
    ]

    assert len(rejected) == 1
    assert "query" in rejected[0]["error"]
    assert outcome["budget_used"]["tool_calls"] == 0


# ── 预算与收敛 ────────────────────────────────────────────────────────────────

def test_repeating_the_same_call_converges_instead_of_looping() -> None:
    planner = ScriptedPlanner(
        [decision(SEARCH_FAQ, query="二类户", top_k=3)] * 6  # 无限重复同一步
    )

    outcome = ask(MATERIALS_QUESTION, planner=planner)

    assert outcome["stop_reason"] == "repeat_call"
    assert outcome["truncated"] is True
    # 第 3 次被拦下，前两次照常执行
    assert outcome["budget_used"]["tool_calls"] == 2


def test_step_budget_truncates_without_raising() -> None:
    planner = ScriptedPlanner(
        [decision(SEARCH_FAQ, query=f"二类户 {index}", top_k=3) for index in range(6)]
    )

    outcome = ask(
        MATERIALS_QUESTION,
        planner=planner,
        budget=KnowledgeBudget(max_steps=2, max_tokens=10_000),
    )

    assert outcome["stop_reason"] == "max_steps"
    assert outcome["truncated"] is True
    assert outcome["budget_used"]["steps"] == 2


def test_the_knowledge_budget_is_tighter_than_the_review_budget_by_default() -> None:
    """客服问题域更浅，默认步数比审核侧少 —— 这是策略，不是手滑。"""
    from ai_service.agent import AgentBudget

    assert KnowledgeBudget().max_steps < AgentBudget().max_steps
    assert isinstance(KnowledgeBudget(), AgentBudget)


# ── 每一步都能降级 ────────────────────────────────────────────────────────────

def test_without_a_model_the_agent_still_answers_from_the_corpus() -> None:
    outcome = ask(MATERIALS_QUESTION)

    assert outcome["engine"]["decision"] == "rule"
    assert outcome["stop_reason"] == "finished"
    assert outcome["citations"]
    assert "所需材料" in outcome["answer"]
    assert "办理流程" in outcome["answer"]


def test_unparseable_model_output_degrades_instead_of_failing() -> None:
    class ProsePlanner:
        @property
        def available(self) -> bool:
            return True

        @property
        def name(self) -> str:
            return "llm:prose"

        async def complete(self, prompt: str, **_: Any) -> str:
            return "这个问题我觉得应该去营业网点办理。"  # 散文，不是 JSON

    outcome = ask(MATERIALS_QUESTION, planner=ProsePlanner())

    assert outcome["engine"]["decision"] == "rule"
    assert outcome["citations"]
    assert any(
        entry.get("step") == "decision_failed" and entry.get("reason") == "unparseable"
        for entry in outcome["trace"]
    )


def test_both_paths_produce_the_same_shape() -> None:
    """两条路径结构必须一致，否则前端要为「降级」多写一套分支。"""
    offline = ask(MATERIALS_QUESTION)
    with_model = ask(
        MATERIALS_QUESTION,
        planner=ScriptedPlanner(
            [decision(SEARCH_FAQ, query="二类户 材料", top_k=4), finish_step("已足够。")]
        ),
    )

    assert set(offline) == set(with_model)
    for outcome in (offline, with_model):
        for key in (
            "answer",
            "citations",
            "actions",
            "grounding",
            "token_usage",
            "budget_used",
            "trace",
            "disclaimer",
        ):
            assert key in outcome


def test_every_executed_step_carries_the_required_trace_fields() -> None:
    outcome = ask(MATERIALS_QUESTION, planner=None)
    steps = [entry for entry in outcome["trace"] if entry.get("executed")]

    assert steps
    for entry in steps:
        for key in ("step", "thought", "tool", "params", "ok", "observation", "latency_ms"):
            assert key in entry


# ── 去偏：别拿无关文档拼答复 ──────────────────────────────────────────────────

def test_off_topic_documents_are_dropped_before_they_become_evidence() -> None:
    """本服务检索分数是相对归一化的（最高分恒为 1.0），
    所以「不相关」必须靠词面覆盖率判，不能靠分数。"""
    outcome = ask(OFF_TOPIC_QUESTION)

    assert outcome["off_topic_dropped"] > 0
    assert outcome["citations"] == []


def test_product_questions_do_not_pull_in_unrelated_faq_documents() -> None:
    outcome = ask("信用卡和借记卡有什么区别？")

    titles = " ".join(item["title"] for item in outcome["citations"])
    assert "信用卡" in titles
    assert "一类账户和二类账户" not in titles


def test_product_comparison_returns_both_products() -> None:
    """问的是两张卡的差异，只给一张等于答了一半。"""
    outcome = ask("信用卡和借记卡有什么区别？")

    ids = {item["doc_id"] for item in outcome["citations"]}
    assert "kb.product.credit_card" in ids
    assert "kb.product.debit_card" in ids


# ── 入参脱敏 ──────────────────────────────────────────────────────────────────

def test_pasted_sensitive_numbers_never_reach_the_model() -> None:
    planner = ScriptedPlanner([finish_step("这是通用说明。")])
    raw_card = "6222 0202 0202 0001"

    outcome = ask(f"帮我看看 {raw_card} 这张卡怎么用", planner=planner)
    prompts = "\n".join(planner.prompts)

    assert outcome["sanitized"] == ["银行卡号"]
    assert "6222" not in prompts
    assert "6222" not in json.dumps(outcome, ensure_ascii=False)


def test_an_ordinary_question_is_not_touched_by_the_sanitizer() -> None:
    clean, hit = policy.sanitize_question(MATERIALS_QUESTION)

    assert clean == MATERIALS_QUESTION
    assert hit == []


# ── HTTP 接口 ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(create_knowledge_router())
    return TestClient(app)


def test_ask_endpoint_answers_a_business_question(client: TestClient) -> None:
    response = client.post("/knowledge/ask", json={"question": MATERIALS_QUESTION})
    payload = response.json()

    assert response.status_code == 200
    assert payload["answer"]
    assert payload["citations"]
    assert payload["disclaimer"]


def test_ask_endpoint_is_separate_from_the_review_endpoints(client: TestClient) -> None:
    """客服与审核的服务对象不同，入口必须分开。"""
    paths = set(client.app.openapi()["paths"])  # type: ignore[attr-defined]

    assert "/knowledge/ask" in paths
    assert "/explain" not in paths


def test_ask_endpoint_rejects_an_empty_question(client: TestClient) -> None:
    assert client.post("/knowledge/ask", json={"question": ""}).status_code == 422


def test_ask_endpoint_rejects_an_over_long_question(client: TestClient) -> None:
    assert (
        client.post("/knowledge/ask", json={"question": "问" * 2000}).status_code == 422
    )


def test_ask_endpoint_reports_health(client: TestClient) -> None:
    payload = client.get("/knowledge/health").json()

    assert payload["status"] == "ok"
    assert payload["surface"] == "knowledge"
    assert "llm_available" in payload


def test_an_out_of_scope_question_is_still_http_200(client: TestClient) -> None:
    """「拒答」是正常业务结局，不是错误 —— 做成 4xx 会诱导调用方重试，
    而重试同一个问题也不会变得能回答。"""
    response = client.post("/knowledge/ask", json={"question": "我额度能提多少？"})

    assert response.status_code == 200
    assert response.json()["refused"] is True


# ── 入口便捷函数 ──────────────────────────────────────────────────────────────

def test_run_knowledge_ask_matches_the_agent_output() -> None:
    direct = ask(MATERIALS_QUESTION)
    via_helper = run(run_knowledge_ask(MATERIALS_QUESTION))

    assert set(direct) == set(via_helper)
    assert via_helper["stop_reason"] == direct["stop_reason"]


def test_a_model_that_calls_handoff_short_circuits_the_loop() -> None:
    planner = ScriptedPlanner(
        [
            decision(
                "handoff_to_human",
                reason="知识库没有依据",
                missing_evidence=["可核对的依据"],
            )
        ]
    )

    outcome = ask("某个我们不掌握的冷门业务问题", planner=planner)

    assert outcome["stop_reason"] == "escalated"
    assert outcome["handoff"]["escalated"] is True
    assert HANDOFF_TO_HUMAN in tools_used(outcome)


def test_lookup_product_is_available_and_reads_only_the_corpus() -> None:
    """产品查询走的是固定语料，不连任何实时系统。"""
    outcome = ask("手机银行有哪些功能", planner=None)

    assert LOOKUP_PRODUCT in KNOWLEDGE_TOOL_WHITELIST
    assert isinstance(NullLLMClient(), NullLLMClient)  # 显式声明：离线路径不需要 key
