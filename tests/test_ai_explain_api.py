"""API tests for ``/ai/explain`` and ``/ai/status``.

The HTTP transport of the AI client is stubbed, so these tests exercise the real
request building, masking, normalisation and degradation paths without ever
touching the network.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai_client import AIAssistClient, set_ai_client
from app.review_records import save_review_record

RECORD_REQUEST_ID = "ai-test-record"
EXPLAIN_URL = f"/ai/explain/{RECORD_REQUEST_ID}"

AI_RESPONSE: dict[str, Any] = {
    "explanation": "本次结论为待复核，根因是影像模糊导致有效期未被识别。",
    "reason_details": [
        {
            "code": "image_blur",
            "known": True,
            "title": "image_blur 图片模糊",
            "root_cause": "quality",
            "meaning": "模糊会导致 OCR 识别错误。",
            "trigger": "清晰度过低。",
            "implementation": "app/quality_check.py detect_blur()，拉普拉斯方差小于 80.0。",
            "advice": "让用户重新拍摄。",
            "user_message": "图片有些模糊，请重新拍摄。",
        }
    ],
    "actions": ["先修影像质量，字段缺失大概率是下游后果。"],
    "citations": [
        {
            "doc_id": "rc.image_blur",
            "title": "image_blur 图片模糊",
            "category": "reason_code",
            "score": 1.0,
            "snippet": "原因码 image_blur，含义是图片模糊。",
        }
    ],
    "unknown_reason_codes": [],
    "confidence": 0.8,
    "degraded": False,
    "engine": {
        "llm": "llm:openai:gpt-4o-mini",
        "llm_available": True,
        "retrieval": "hashing-ngram-512",
        "generation": "llm",
        "rewrite": "llm",
        "rerank": "llm",
    },
    "latency_ms": 412.5,
    "trace": [],
    "disclaimer": "仅供复核参考。",
}


class StubTransportClient(AIAssistClient):
    """Real client logic with only the HTTP transport replaced."""

    def __init__(self, response: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stub_response = response
        self.sent_payloads: list[dict[str, Any] | None] = []
        self.requested_paths: list[str] = []

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, str]:
        self.requested_paths.append(path)
        self.sent_payloads.append(body)
        return dict(self._stub_response), ""


class FailingTransportClient(AIAssistClient):
    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, str]:
        return None, "unreachable"


def seed_review_record() -> None:
    save_review_record(
        request_id=RECORD_REQUEST_ID,
        doc_type="bank_card",
        filename="synthetic-card.png",
        ocr_mode="mock",
        review_result="review",
        quality_result="review",
        quality_reasons=["image_blur"],
        review_reasons=["missing_valid_date", "image_blur"],
        fields={"card_number": "6222021234567890", "name": "张三", "valid_date": "08/29"},
    )


# ── 鉴权与 CSRF ───────────────────────────────────────────────────────────────

def test_explain_requires_authentication(isolated_auth_client: TestClient) -> None:
    response = isolated_auth_client.post(EXPLAIN_URL)

    assert response.status_code == 401


def test_explain_rejects_non_administrator(authenticated_client: TestClient) -> None:
    response = authenticated_client.post(EXPLAIN_URL)

    assert response.status_code == 403
    assert response.json()["detail"] == "Administrator access required."


def test_explain_requires_csrf_token(authenticated_admin_client: TestClient) -> None:
    authenticated_admin_client.headers.pop("X-CSRF-Token", None)

    response = authenticated_admin_client.post(EXPLAIN_URL)

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed."


def test_ai_status_requires_administrator(authenticated_client: TestClient) -> None:
    assert authenticated_client.get("/ai/status").status_code == 403


# ── 记录解析 ──────────────────────────────────────────────────────────────────

def test_explain_returns_404_for_unknown_record(
    authenticated_admin_client: TestClient,
) -> None:
    response = authenticated_admin_client.post("/ai/explain/does-not-exist")

    assert response.status_code == 404
    assert response.json()["detail"] == "Review record not found."


# ── 成功路径 ──────────────────────────────────────────────────────────────────

def test_explain_returns_ai_payload_for_an_existing_record(
    authenticated_admin_client: TestClient,
) -> None:
    set_ai_client(StubTransportClient(AI_RESPONSE))
    seed_review_record()

    response = authenticated_admin_client.post(EXPLAIN_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["degraded"] is False
    assert body["request_id"] == RECORD_REQUEST_ID
    assert body["reason_details"][0]["code"] == "image_blur"
    assert body["actions"] == ["先修影像质量，字段缺失大概率是下游后果。"]
    assert body["engine"]["generation"] == "llm"


def test_explain_sends_masked_fields_to_the_ai_service(
    authenticated_admin_client: TestClient,
) -> None:
    stub = StubTransportClient(AI_RESPONSE)
    set_ai_client(stub)
    seed_review_record()

    authenticated_admin_client.post(EXPLAIN_URL)

    assert stub.requested_paths == ["/explain"]
    sent = stub.sent_payloads[0]
    assert sent is not None
    assert sent["fields"]["card_number"] == "622202******7890"
    assert sent["fields"]["name"] == "张*"
    assert sent["fields"]["valid_date"] == "08/29"
    assert sent["review_reasons"] == ["missing_valid_date", "image_blur"]
    assert sent["quality_reasons"] == ["image_blur"]
    assert sent["request_id"] == RECORD_REQUEST_ID


# ── 降级路径 ──────────────────────────────────────────────────────────────────

def test_explain_degrades_to_200_when_ai_service_is_unreachable(
    authenticated_admin_client: TestClient,
) -> None:
    set_ai_client(FailingTransportClient(timeout_s=0.01))
    seed_review_record()

    response = authenticated_admin_client.post(EXPLAIN_URL)

    # 关键：AI 不可用不能变成 5xx，页面必须还能正常显示记录
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["degraded"] is True
    assert body["reason"] == "unreachable"
    assert body["reason_details"] == []
    assert body["message"]


def test_explain_degrades_cleanly_when_the_switch_is_off(
    authenticated_admin_client: TestClient,
) -> None:
    set_ai_client(AIAssistClient(enabled=False))
    seed_review_record()

    response = authenticated_admin_client.post(EXPLAIN_URL)

    assert response.status_code == 200
    assert response.json()["reason"] == "disabled"


def test_explain_is_idempotent_across_repeated_calls(
    authenticated_admin_client: TestClient,
) -> None:
    stub = StubTransportClient(AI_RESPONSE)
    set_ai_client(stub)
    seed_review_record()

    first = authenticated_admin_client.post(EXPLAIN_URL)
    second = authenticated_admin_client.post(EXPLAIN_URL)

    assert first.json() == second.json()
    assert stub.requested_paths == ["/explain", "/explain"]


# ── 状态接口 ──────────────────────────────────────────────────────────────────

def test_ai_status_reports_client_state_without_probing(
    authenticated_admin_client: TestClient,
) -> None:
    stub = StubTransportClient({"status": "ok"})
    set_ai_client(stub)

    response = authenticated_admin_client.get("/ai/status")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["circuit_state"] == "closed"
    assert body["service"] is None
    assert stub.requested_paths == []


def test_ai_status_probes_the_service_on_request(
    authenticated_admin_client: TestClient,
) -> None:
    stub = StubTransportClient({"status": "ok", "chunk_count": 42, "llm_available": False})
    set_ai_client(stub)

    response = authenticated_admin_client.get("/ai/status?probe=true")

    assert response.status_code == 200
    assert response.json()["service"]["chunk_count"] == 42
    assert stub.requested_paths == ["/health"]


@pytest.mark.parametrize("path", ["/ai/status", "/ai/status?probe=true"])
def test_ai_status_is_not_available_to_anonymous_callers(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    assert isolated_auth_client.get(path).status_code == 401
