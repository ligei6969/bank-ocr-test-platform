"""Tests for offline-safe AI metrics and the Prometheus endpoint."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from ai_service.api import create_app
from ai_service.metrics import AgentMetrics


def sample_outcome(**overrides: Any) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "degraded": True,
        "truncated": False,
        "stop_reason": "finished",
        "latency_ms": 125,
        "token_usage": {
            "source": "provider",
            "total": 17,
            "llm_calls": 2,
        },
        "trace": [
            {"tool": "search_knowledge", "executed": True, "ok": True, "latency_ms": 25},
            {"tool": "secret_customer_tool", "executed": True, "ok": False, "latency_ms": 9},
        ],
        "tools": {
            "search_knowledge": {"circuit": {"state": "closed"}},
            "secret_customer_tool": {"circuit": {"state": "open"}},
        },
    }
    outcome.update(overrides)
    return outcome


def test_registry_exposes_bounded_prometheus_metrics_without_request_data() -> None:
    metrics = AgentMetrics()
    metrics.observe("review_agent", sample_outcome())
    rendered = metrics.render()

    assert '# TYPE bank_ocr_agent_requests_total counter' in rendered
    assert 'bank_ocr_agent_requests_total{surface="review_agent",result="success"} 1' in rendered
    assert 'bank_ocr_agent_degraded_total{surface="review_agent"} 1' in rendered
    assert 'bank_ocr_agent_tokens_total{surface="review_agent",source="provider"} 17' in rendered
    assert 'bank_ocr_tool_calls_total{tool="search_knowledge",status="success"} 1' in rendered
    assert 'bank_ocr_tool_calls_total{tool="other",status="failure"} 1' in rendered
    assert 'bank_ocr_tool_circuit_state{tool="search_knowledge",state="closed"} 1' in rendered
    assert 'le="0.25"} 1' in rendered
    assert "secret_customer_tool" not in rendered
    assert "request_id" not in rendered
    assert "question" not in rendered


def test_unknown_surface_is_bucketed_without_crashing() -> None:
    metrics = AgentMetrics()
    metrics.observe("future_surface", sample_outcome())

    rendered = metrics.render()

    assert 'surface="other",result="success"' in rendered
    assert 'surface="other",le="+Inf"} 1' in rendered


def test_cached_tool_events_are_counted_as_successful_calls() -> None:
    metrics = AgentMetrics()
    metrics.observe(
        "review_agent",
        sample_outcome(
            trace=[{"step": "tool_cache_hit", "tool": "search_knowledge"}],
            tools={},
        ),
    )

    assert 'bank_ocr_tool_calls_total{tool="search_knowledge",status="success"} 1' in metrics.render()


    metrics = AgentMetrics()
    metrics.observe(
        "knowledge",
        sample_outcome(
            refused=True,
            truncated=True,
            stop_reason="attacker-controlled-reason",
            token_usage={"source": "estimate", "total": 8, "llm_calls": 1},
            trace=[],
            tools={},
        ),
    )
    rendered = metrics.render()

    assert 'result="refused"} 1' in rendered
    assert 'reason="other"} 1' in rendered
    assert 'source="estimate"} 8' in rendered
    assert "attacker-controlled-reason" not in rendered


def test_histogram_family_is_declared_once_and_uses_cumulative_buckets() -> None:
    metrics = AgentMetrics()
    metrics.observe("explain", sample_outcome(latency_ms=50, trace=[], tools={}))
    rendered = metrics.render()

    assert rendered.count("# TYPE bank_ocr_agent_duration_seconds histogram") == 1
    assert 'bank_ocr_agent_duration_seconds_bucket{surface="explain",le="0.05"} 1' in rendered
    assert 'bank_ocr_agent_duration_seconds_bucket{surface="explain",le="0.1"} 1' in rendered
    assert 'bank_ocr_agent_duration_seconds_bucket{surface="explain",le="0.025"} 0' in rendered
    assert 'bank_ocr_agent_duration_seconds_count{surface="explain"} 1' in rendered


def test_metrics_endpoint_requires_token_when_bound_or_configured(monkeypatch: Any) -> None:
    monkeypatch.setenv("AI_SERVICE_HOST", "0.0.0.0")
    monkeypatch.setenv("AI_METRICS_TOKEN", "metrics-secret")
    client = TestClient(create_app())

    assert client.get("/metrics").status_code == 403
    assert client.get("/metrics", headers={"Authorization": "Bearer metrics-secret"}).status_code == 200


def test_metrics_endpoint_is_local_only_by_default() -> None:
    client = TestClient(create_app())

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "# HELP bank_ocr_agent_requests_total" in response.text
    assert "# TYPE bank_ocr_agent_duration_seconds histogram" in response.text
    assert 'surface="review_agent"' in response.text
    assert 'surface="review_agent",le="+Inf"} 0' in response.text


def test_agent_500_is_counted_without_leaking_request_data(monkeypatch: Any) -> None:
    async def fail(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("secret failure detail")

    from ai_service import api as api_module

    monkeypatch.setattr(api_module, "run_agent_for_context", fail)
    client = TestClient(create_app())
    response = client.post("/agent/explain", json={"request_id": "sensitive", "doc_type": "bank_card"})
    rendered = client.get("/metrics").text

    assert response.status_code == 500
    assert 'bank_ocr_agent_requests_total{surface="review_agent",result="error"} 1' in rendered
    assert "sensitive" not in rendered
    assert "secret failure detail" not in rendered


def test_agent_endpoint_updates_metrics_without_exposing_request_id() -> None:
    client = TestClient(create_app())
    payload = {
        "request_id": "sensitive-request-identifier",
        "doc_type": "bank_card",
        "review_result": "review",
        "quality_result": "review",
        "quality_reasons": ["image_blur"],
        "review_reasons": ["missing_valid_date", "image_blur"],
        "fields": {"card_number": "6222******7890", "name": "张*"},
        "question": "这张卡为什么需要人工复核？",
    }

    response = client.post("/agent/explain", json=payload)
    rendered = client.get("/metrics").text

    assert response.status_code == 200
    assert 'bank_ocr_agent_requests_total{surface="review_agent",result="success"} 1' in rendered
    assert "sensitive-request-identifier" not in rendered
    assert "张*" not in rendered


def test_knowledge_endpoint_updates_its_own_surface_metrics() -> None:
    client = TestClient(create_app())

    response = client.post("/knowledge/ask", json={"question": "办理二类账户需要哪些材料？"})
    rendered = client.get("/metrics").text

    assert response.status_code == 200
    assert 'bank_ocr_agent_requests_total{surface="knowledge",result="success"} 1' in rendered
    assert 'bank_ocr_agent_degraded_total{surface="knowledge"} 1' in rendered
