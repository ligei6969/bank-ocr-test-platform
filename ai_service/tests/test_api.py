"""AI 服务自身 HTTP 接口的测试。

P0 只测到了 ``build_explainer`` 这一层，``create_app`` 本身没有被直接测过 ——
端点拼错、模型校验漏掉、响应结构变了都不会被发现。这里补上。

全部离线：``create_app()`` 在没有 LLM 配置时构造 ``NullLLMClient``，
降级路径照样给出完整结果。CI 不联网这条约束靠 ``tests/conftest.py`` 兜住。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi.testclient import TestClient

from ai_service.api import create_app
from ai_service.tools import TOOL_WHITELIST


def client() -> TestClient:
    return TestClient(create_app())


def payload(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "request_id": "req-api-1",
        "doc_type": "bank_card",
        "review_result": "review",
        "quality_result": "review",
        "quality_reasons": ["image_blur"],
        "review_reasons": ["missing_valid_date", "image_blur"],
        "fields": {"card_number": "6222******7890", "name": "张*"},
        "question": "这张卡为什么需要人工复核？",
        "top_k": 5,
    }
    base.update(overrides)
    return base


# ── /health ───────────────────────────────────────────────────────────────────

def test_health_reports_agent_capability() -> None:
    body = client().get("/health").json()

    assert body["status"] == "ok"
    assert body["llm_available"] is False
    assert body["agent"]["endpoint"] == "/agent/explain"
    assert set(body["agent"]["tools"]) == set(TOOL_WHITELIST)
    assert body["doc_count"] > 0


# ── /explain（P0 固定流水线）──────────────────────────────────────────────────

def test_explain_still_works_offline() -> None:
    response = client().post("/explain", json=payload())

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["engine"]["generation"] == "template"
    assert body["reason_details"]


def test_explain_rejects_unknown_doc_type() -> None:
    response = client().post("/explain", json=payload(doc_type="passport"))

    assert response.status_code == 422


# ── /agent/explain（P1 多步决策）──────────────────────────────────────────────

def test_agent_endpoint_runs_offline_with_the_deterministic_sequence() -> None:
    response = client().post("/agent/explain", json=payload())

    assert response.status_code == 200
    body = response.json()

    assert body["engine"]["decision"] == "rule"
    assert body["stop_reason"] == "finished"
    assert body["truncated"] is False
    executed = [entry["tool"] for entry in body["trace"] if entry.get("executed")]
    assert executed == ["get_review_record", "search_knowledge", "recompute_quality"]


def test_agent_endpoint_returns_the_full_contract() -> None:
    body = client().post("/agent/explain", json=payload()).json()

    for key in (
        "request_id",
        "answer",
        "explanation",
        "reason_details",
        "actions",
        "citations",
        "escalation",
        "quality_audit",
        "degraded",
        "truncated",
        "stop_reason",
        "engine",
        "budget",
        "budget_used",
        "prompt_versions",
        "latency_ms",
        "trace",
        "tools",
        "disclaimer",
    ):
        assert key in body, key

    assert body["answer"] == body["explanation"]


def test_agent_endpoint_escalates_when_evidence_is_missing() -> None:
    response = client().post(
        "/agent/explain",
        json=payload(quality_reasons=[], review_reasons=[], quality_result=None),
    )

    body = response.json()
    assert body["stop_reason"] == "escalated"
    assert body["escalation"]["escalated"] is True


def test_agent_endpoint_rejects_unknown_doc_type() -> None:
    response = client().post("/agent/explain", json=payload(doc_type="passport"))

    assert response.status_code == 422


def test_agent_endpoint_rejects_a_malformed_request_body() -> None:
    """字段类型不对属于**请求契约**问题，应在入口 422 拦掉，不进 Agent。

    这是有意的：契约层拒绝 vs Agent 内部兜底是两件事。前者说明调用方有 bug，
    应该立刻炸给调用方看；后者是运行期不确定性，该降级。
    """
    response = client().post(
        "/agent/explain",
        json=payload(review_reasons="not-a-list", quality_reasons=42, fields="nope"),
    )

    assert response.status_code == 422


def test_agent_endpoint_survives_an_unusual_but_valid_context() -> None:
    """形状合法但语义古怪的上下文（空原因码、未知 doc_type 之外的怪值）不能 5xx。"""
    response = client().post(
        "/agent/explain",
        json=payload(
            review_result="",
            quality_result=None,
            quality_reasons=[],
            review_reasons=[],
            fields={},
            error_message=None,
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"]
    # 什么都没有时应当转人工，而不是硬编一个结论
    assert body["stop_reason"] == "escalated"


# ── /search 与 /tools/stats ───────────────────────────────────────────────────

def test_search_endpoint_returns_hits() -> None:
    response = client().post(
        "/search",
        json={"query": "图片模糊", "reason_codes": ["image_blur"], "top_k": 3},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 1
    assert body["hits"][0]["doc_id"]


def test_search_endpoint_caps_top_k() -> None:
    response = client().post("/search", json={"query": "模糊", "top_k": 999})

    assert response.status_code == 422


def test_tool_stats_endpoint_responds() -> None:
    assert client().get("/tools/stats").status_code == 200
