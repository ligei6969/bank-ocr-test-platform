"""Unit tests for the AI review assistant client.

Covers the three guarantees the platform depends on:

1. the call never raises,
2. timeouts and failures degrade instead of propagating,
3. the circuit breaker stops hammering a dead service,
4. sensitive field values are masked before they leave the process.
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any, Callable

import pytest

from app.ai_client import (
    AIAssistClient,
    CircuitState,
    build_ai_client,
)


class FakeResponse:
    """Minimal stand-in for the object returned by ``urlopen``."""

    def __init__(self, payload: Any, status: int = 200) -> None:
        self.status = status
        self._body = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def read(self, size: int = -1) -> bytes:
        return self._body[:size] if size and size > 0 else self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def install_fake_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[Any], Any],
) -> list[dict[str, Any]]:
    """Replace ``urllib.request.urlopen`` and record the outgoing requests."""
    calls: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        body = request.data
        calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "timeout": timeout,
                "payload": json.loads(body.decode("utf-8")) if body else None,
            }
        )
        return handler(request)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def always(payload: Any) -> Callable[[Any], FakeResponse]:
    return lambda _request: FakeResponse(payload)


def raise_error(error: Exception) -> Callable[[Any], Any]:
    def handler(_request: Any) -> Any:
        raise error

    return handler


# ── 开关与降级 ────────────────────────────────────────────────────────────────

def test_disabled_client_degrades_without_touching_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_fake_urlopen(monkeypatch, always({"explanation": "should not be used"}))
    client = AIAssistClient(enabled=False)

    result = client.explain({"request_id": "abc123"})

    assert calls == []
    assert result["available"] is False
    assert result["degraded"] is True
    assert result["reason"] == "disabled"
    assert result["request_id"] == "abc123"
    assert result["reason_details"] == []
    assert result["actions"] == []
    assert "未启用" in result["message"]


def test_degraded_result_always_carries_the_full_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_urlopen(monkeypatch, raise_error(urllib.error.URLError("connection refused")))
    client = AIAssistClient(base_url="http://127.0.0.1:1")

    result = client.explain({"request_id": "abc123"})

    for key in (
        "available",
        "degraded",
        "reason",
        "message",
        "explanation",
        "reason_details",
        "actions",
        "citations",
        "unknown_reason_codes",
        "confidence",
        "engine",
        "latency_ms",
        "trace",
    ):
        assert key in result, key
    assert result["available"] is False
    assert result["confidence"] == 0.0


# ── 成功路径 ──────────────────────────────────────────────────────────────────

def test_successful_explain_is_normalised_for_the_frontend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_fake_urlopen(
        monkeypatch,
        always(
            {
                "explanation": "因为模糊导致有效期未被识别。",
                "reason_details": [{"code": "image_blur", "known": True}],
                "confidence": 0.87,
            }
        ),
    )
    client = AIAssistClient(base_url="http://ai.test:8100")

    result = client.explain({"request_id": "abc123"})

    assert calls[0]["url"] == "http://ai.test:8100/explain"
    assert calls[0]["method"] == "POST"
    assert calls[0]["timeout"] == client.timeout_s
    assert result["available"] is True
    assert result["degraded"] is False
    assert result["explanation"] == "因为模糊导致有效期未被识别。"
    assert result["confidence"] == pytest.approx(0.87)
    # 服务端漏掉的键由客户端补成合法的空值，前端不必判空
    assert result["actions"] == []
    assert result["citations"] == []
    assert result["trace"] == []
    assert result["engine"] == {}


def test_garbage_confidence_is_coerced_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_urlopen(monkeypatch, always({"confidence": "很高"}))
    client = AIAssistClient()

    assert client.explain({"request_id": "abc"})["confidence"] == 0.0


# ── 失败与降级 ────────────────────────────────────────────────────────────────

def test_timeout_degrades_with_timeout_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_urlopen(monkeypatch, raise_error(TimeoutError("timed out")))
    client = AIAssistClient(timeout_s=0.01)

    result = client.explain({"request_id": "abc"})

    assert result["available"] is False
    assert result["reason"] == "timeout"
    assert "超时" in result["message"]


def test_http_error_degrades_with_http_error_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    error = urllib.error.HTTPError(
        url="http://ai.test/explain",
        code=500,
        msg="boom",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    install_fake_urlopen(monkeypatch, raise_error(error))

    result = AIAssistClient().explain({"request_id": "abc"})

    assert result["reason"] == "http_error"
    assert result["available"] is False


def test_non_json_body_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_urlopen(monkeypatch, always(b"<html>not json</html>"))

    result = AIAssistClient().explain({"request_id": "abc"})

    assert result["reason"] == "invalid_response"
    assert result["available"] is False


def test_json_array_body_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_urlopen(monkeypatch, always([1, 2, 3]))

    result = AIAssistClient().explain({"request_id": "abc"})

    assert result["reason"] == "invalid_response"


# ── 熔断 ──────────────────────────────────────────────────────────────────────

def test_circuit_opens_after_configured_failures_and_stops_calling_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_fake_urlopen(monkeypatch, raise_error(urllib.error.URLError("refused")))
    client = AIAssistClient(failure_threshold=3, recovery_s=3600.0)

    for _ in range(3):
        assert client.explain({"request_id": "abc"})["reason"] == "unreachable"

    assert client.status()["circuit_state"] == CircuitState.OPEN
    assert len(calls) == 3

    # 熔断打开后不再发请求，直接返回降级结果
    result = client.explain({"request_id": "abc"})
    assert result["reason"] == "circuit_open"
    assert len(calls) == 3


def test_circuit_half_opens_after_recovery_window_and_closes_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[Any] = [urllib.error.URLError("refused"), urllib.error.URLError("refused")]
    monkeypatch.setattr("urllib.request.urlopen", _scripted(errors))

    client = AIAssistClient(failure_threshold=2, recovery_s=0.0)
    client.explain({"request_id": "abc"})
    client.explain({"request_id": "abc"})
    assert client.status()["circuit_state"] == CircuitState.OPEN

    # recovery_s 为 0，下一次调用进入半开探测；探测成功后电路闭合
    result = client.explain({"request_id": "abc"})

    assert result["available"] is True
    assert result["explanation"] == "recovered"
    assert client.status()["circuit_state"] == CircuitState.CLOSED


def _scripted(errors: list[Any]) -> Callable[..., Any]:
    def fake_urlopen(_request: Any, timeout: float | None = None) -> Any:
        error = errors.pop(0) if errors else None
        if isinstance(error, Exception):
            raise error
        return FakeResponse({"explanation": "recovered"})

    return fake_urlopen


def test_success_resets_the_failure_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_urlopen(monkeypatch, always({"explanation": "ok"}))
    client = AIAssistClient(failure_threshold=3)

    client.explain({"request_id": "abc"})

    assert client.status()["consecutive_failures"] == 0
    assert client.status()["circuit_state"] == CircuitState.CLOSED


def test_reset_clears_breaker_state(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_urlopen(monkeypatch, raise_error(urllib.error.URLError("refused")))
    client = AIAssistClient(failure_threshold=1)

    client.explain({"request_id": "abc"})
    assert client.status()["circuit_state"] == CircuitState.OPEN

    client.reset()

    assert client.status()["circuit_state"] == CircuitState.CLOSED
    assert client.status()["failure_count"] == 0
    assert client.status()["last_error"] is None


# ── 出站脱敏（合规关键路径）───────────────────────────────────────────────────

def test_sensitive_values_are_masked_before_the_request_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_fake_urlopen(monkeypatch, always({"explanation": "ok"}))
    client = AIAssistClient()

    client.explain(
        {
            "request_id": "abc",
            "fields": {
                "card_number": "6222021234567890",
                "valid_date": "08/29",
                "name": "张三",
                "id_number": "110101199003071234",
                "address": "北京市朝阳区某某路 1 号",
            },
            "error_message": "rejected card 6222021234567890",
            "question": "卡号 6222021234567890 是什么问题？",
        }
    )

    sent = json.dumps(calls[0]["payload"], ensure_ascii=False)

    assert "6222021234567890" not in sent
    assert "110101199003071234" not in sent
    assert "张三" not in sent
    assert "北京市朝阳区某某路 1 号" not in sent

    sent_fields = calls[0]["payload"]["fields"]
    assert sent_fields["card_number"] == "622202******7890"
    assert sent_fields["name"] == "张*"
    assert sent_fields["valid_date"] == "08/29"


def test_health_returns_unavailable_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_fake_urlopen(monkeypatch, always({"status": "ok"}))

    result = AIAssistClient(enabled=False).health()

    assert result == {"available": False, "reason": "disabled"}
    assert calls == []


def test_health_reports_service_details_when_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_urlopen(
        monkeypatch,
        always({"status": "ok", "llm_available": False, "chunk_count": 42}),
    )

    result = AIAssistClient(base_url="http://ai.test:8100").health()

    assert result["available"] is True
    assert result["chunk_count"] == 42


# ── 环境变量解析 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("", True),  # 空值回落到默认值
    ],
)
def test_enabled_flag_parsing(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("AI_ASSIST_ENABLED", raw)

    assert build_ai_client().enabled is expected


def test_service_url_and_timeout_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_SERVICE_URL", "http://ai.internal:9000/")
    monkeypatch.setenv("AI_ASSIST_TIMEOUT_S", "1.5")
    monkeypatch.setenv("AI_ASSIST_FAILURE_THRESHOLD", "7")
    monkeypatch.setenv("AI_ASSIST_RECOVERY_S", "30")

    client = build_ai_client()

    assert client.base_url == "http://ai.internal:9000"
    assert client.timeout_s == pytest.approx(1.5)
    assert client.failure_threshold == 7
    assert client.recovery_s == pytest.approx(30.0)


def test_invalid_numeric_environment_values_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_ASSIST_TIMEOUT_S", "not-a-number")
    monkeypatch.setenv("AI_ASSIST_FAILURE_THRESHOLD", "")

    client = build_ai_client()

    assert client.timeout_s == pytest.approx(3.0)
    assert client.failure_threshold == 3
