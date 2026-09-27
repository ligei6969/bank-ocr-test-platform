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


# ── 双判改判率（P2.3 / P3）──────────────────────────────────────────────────

def test_adjudication_counts_exclude_degraded_from_the_denominator() -> None:
    """降级的复核没有「建议」可言，不能进改判率的分母。

    回归：把降级算进分母会让改判率被故障稀释，而且方向是反的 ——
    故障越多改判率越低，看起来像规则阈值的问题，实际是 AI 服务不可用。
    """
    metrics = AgentMetrics()
    for _ in range(12):
        metrics.observe("adjudicate", {"decision": "review", "degraded": False})
    for _ in range(8):
        metrics.observe("adjudicate", {"decision": "pass", "degraded": False})
    for _ in range(10):
        metrics.observe("adjudicate", {"decision": "review", "degraded": True})

    total, overrides, answered = metrics.adjudication_counts()

    assert total == 30          # 全部复核
    assert overrides == 8
    assert answered == 20       # 分母只算拿到模型结论的


def test_override_rate_is_rendered_as_a_gauge() -> None:
    metrics = AgentMetrics()
    for _ in range(3):
        metrics.observe("adjudicate", {"decision": "pass", "degraded": False})
    for _ in range(1):
        metrics.observe("adjudicate", {"decision": "review", "degraded": False})

    output = metrics.render()

    assert "bank_ocr_adjudication_override_rate 0.75" in output
    assert 'bank_ocr_adjudications_total{decision="pass",result="answered"} 3' in output


def test_override_rate_is_absent_when_nothing_was_reviewed() -> None:
    """一次复核都没跑时不该暴露一个 0 —— 0 会被读成「AI 从不改判」。"""
    output = AgentMetrics().render()

    assert "bank_ocr_adjudication_override_rate" not in output


# ── Z-score 异常检测 ────────────────────────────────────────────────────────

def test_zscore_is_none_when_samples_are_too_few() -> None:
    """样本不足返回 None（算不出来），而不是 0（正常）—— 两者必须区分。"""
    from ai_service.metrics import override_rate_zscore

    assert override_rate_zscore(2, 3, baseline_rate=0.2, baseline_samples=200) is None
    assert override_rate_zscore(10, 50, baseline_rate=0.2, baseline_samples=5) is None


def test_alert_fires_when_the_rate_spikes() -> None:
    from ai_service.metrics import override_rate_alert

    alert = override_rate_alert(20, 40, baseline_rate=0.2, baseline_samples=200)

    assert alert is not None
    assert "飙升" in alert
    assert "Z=" in alert


def test_alert_is_silent_at_the_baseline_rate() -> None:
    from ai_service.metrics import override_rate_alert

    assert override_rate_alert(8, 40, baseline_rate=0.2, baseline_samples=200) is None


def test_a_drop_also_alerts_with_the_opposite_wording() -> None:
    """改判率骤降同样要报 —— AI 可能已经不干活了。"""
    from ai_service.metrics import override_rate_alert

    alert = override_rate_alert(0, 200, baseline_rate=0.3, baseline_samples=200)

    assert alert is not None
    assert "骤降" in alert


def test_zero_baseline_makes_any_override_anomalous() -> None:
    """基线恒为 0 时标准差也是 0，除法无意义 —— 用哨兵值而不是崩掉。"""
    from ai_service.metrics import override_rate_alert, override_rate_zscore

    assert override_rate_zscore(0, 50, baseline_rate=0.0, baseline_samples=200) == 0.0
    alert = override_rate_alert(1, 50, baseline_rate=0.0, baseline_samples=200)
    assert alert is not None and "飙升" in alert


# ── /metrics 端点上的改判率异常告警 ─────────────────────────────────────────

def test_metrics_endpoint_omits_the_baseline_gauges_without_configuration(monkeypatch) -> None:
    """没配基线就不输出该 gauge —— 拿当前值当基线会让它永远显示「正常」。"""
    monkeypatch.delenv("AI_OVERRIDE_BASELINE_RATE", raising=False)
    client = TestClient(create_app())

    body = client.get("/metrics").text

    assert "bank_ocr_adjudication_anomaly" not in body


def test_metrics_endpoint_flags_an_anomalous_override_rate(monkeypatch) -> None:
    monkeypatch.setenv("AI_OVERRIDE_BASELINE_RATE", "0.05")
    monkeypatch.setenv("AI_OVERRIDE_BASELINE_SAMPLES", "200")
    app = create_app()
    metrics = app.state.agent_metrics
    for _ in range(30):
        metrics.observe("adjudicate", {"decision": "pass", "degraded": False})

    body = TestClient(app).get("/metrics").text

    assert "bank_ocr_adjudication_anomaly 1" in body
    assert "bank_ocr_adjudication_override_rate_zscore" in body


def test_metrics_endpoint_does_not_flag_a_normal_rate(monkeypatch) -> None:
    monkeypatch.setenv("AI_OVERRIDE_BASELINE_RATE", "0.5")
    monkeypatch.setenv("AI_OVERRIDE_BASELINE_SAMPLES", "200")
    app = create_app()
    metrics = app.state.agent_metrics
    for _ in range(15):
        metrics.observe("adjudicate", {"decision": "pass", "degraded": False})
    for _ in range(15):
        metrics.observe("adjudicate", {"decision": "review", "degraded": False})

    body = TestClient(app).get("/metrics").text

    assert "bank_ocr_adjudication_anomaly 0" in body


def test_metrics_endpoint_ignores_a_non_numeric_baseline(monkeypatch) -> None:
    """配错格式时忽略并继续，而不是 500 —— 指标端点不该因配置错误挂掉。"""
    monkeypatch.setenv("AI_OVERRIDE_BASELINE_RATE", "二十个点")

    response = TestClient(create_app()).get("/metrics")

    assert response.status_code == 200
    assert "bank_ocr_adjudication_anomaly" not in response.text
