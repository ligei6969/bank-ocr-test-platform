"""Tests for the dual-judge orchestration layer.

核心是**安全不变式**：规则拥有否决权，LLM 只有有限的放行建议权。
这些性质由业务层 ``apply_adjudication`` 兜底（不只是靠 schema），
所以必须逐条钉住 —— 尤其是「AI 无论返回什么都不该改 reject」。
"""

from __future__ import annotations

import pytest

from app.adjudication import apply_adjudication, maybe_adjudicate


class FakeClient:
    """假 AI 客户端：记录被调了几次，返回预设意见。"""

    def __init__(self, decision: str = "pass", *, reason: str = "", rationale: str = "ok", raise_exc=None):
        self.decision = decision
        self.reason = reason
        self.rationale = rationale
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def adjudicate(self, payload: dict) -> dict:
        self.calls.append(payload)
        if self.raise_exc:
            raise self.raise_exc
        return {
            "decision": self.decision,
            "overrode": self.decision == "pass",
            "degraded": bool(self.reason),
            "reason": self.reason,
            "rationale": self.rationale,
        }


BOUNDARY_QUALITY = {"quality_metrics": {"glare_component_ratio": 0.0066}}
GLARE_ONLY = ["glare_detected"]


def _run(client, *, review_result="review", reasons=GLARE_ONLY, quality=BOUNDARY_QUALITY,
         fields=None, doc_type="bank_card"):
    return maybe_adjudicate(
        client=client,
        request_id="req-1",
        doc_type=doc_type,
        review_result=review_result,
        review_reasons=reasons,
        fields=fields or {},
        quality=quality,
    )


# ── 五条安全不变式 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("ai_decision", ["pass", "review", "reject", "", "garbage", None])
def test_reject_is_never_changed_regardless_of_the_model(ai_decision: str | None) -> None:
    """不变式 2：rule=reject 时最终结论恒为 reject，且 AI 根本不该被调用。"""
    client = FakeClient(decision=str(ai_decision or ""))

    final, record = _run(client, review_result="reject")

    assert final == "reject"
    assert client.calls == []
    assert record["llm_invoked"] is False


@pytest.mark.parametrize("ai_decision", ["pass", "review", "reject", "garbage"])
def test_pass_is_never_changed_regardless_of_the_model(ai_decision: str) -> None:
    """不变式 1：rule=pass 时最终结论恒为 pass，且 AI 不该被调用。"""
    client = FakeClient(decision=ai_decision)

    final, record = _run(client, review_result="pass")

    assert final == "pass"
    assert client.calls == []
    assert record["llm_invoked"] is False


def test_non_boundary_review_does_not_call_the_model() -> None:
    """不变式 3：review 但非边界样本 → 维持 review，不调 AI。"""
    client = FakeClient(decision="pass")

    final, record = _run(client, reasons=["image_blur"], quality={"quality_metrics": {}})

    assert final == "review"
    assert client.calls == []
    assert record["llm_invoked"] is False
    assert record["boundary_criteria"] == []


def test_boundary_review_with_ai_review_keeps_review() -> None:
    """不变式 4：边界 + AI 也说要复核 → 维持 review。"""
    client = FakeClient(decision="review")

    final, record = _run(client)

    assert final == "review"
    assert record["llm_invoked"] is True
    assert record["llm_override"] is False


def test_boundary_review_with_ai_pass_becomes_pass() -> None:
    """不变式 5：边界 + AI 认为可放行 → land pass 且标记 override。"""
    client = FakeClient(decision="pass", rationale="反光在镭射区")

    final, record = _run(client)

    assert final == "pass"
    assert record["llm_override"] is True
    assert record["llm_decision"] == "pass"
    assert record["llm_rationale"] == "反光在镭射区"
    assert record["boundary_criteria"] == ["false_positive_reason_code"]


# ── apply_adjudication 本身：绝不 return ai_decision ────────────────────────

@pytest.mark.parametrize(
    ("rule", "ai", "expected"),
    [
        ("reject", "pass", "reject"),      # AI 无权放行 reject
        ("reject", "review", "reject"),
        ("pass", "review", "pass"),        # AI 无权加重 pass
        ("pass", "reject", "pass"),
        ("review", "pass", "pass"),        # 唯一允许的改判方向
        ("review", "review", "review"),
        ("review", "reject", "review"),    # AI 无权加重 review
        ("review", "banana", "review"),    # 非预期值一律原样返回
        ("review", "", "review"),
        ("review", None, "review"),
    ],
)
def test_apply_adjudication_is_a_whitelist_not_a_passthrough(
    rule: str, ai: str | None, expected: str
) -> None:
    assert apply_adjudication(rule, ai) == expected


# ── 失败一律回落 ────────────────────────────────────────────────────────────

def test_client_exception_falls_back_to_rule_verdict() -> None:
    """复核是增强，不能让审核链路因它出错。"""
    client = FakeClient(raise_exc=RuntimeError("AI 挂了"))

    final, record = _run(client)

    assert final == "review"
    assert record["llm_invoked"] is True
    assert record["llm_override"] is False
    assert record["llm_fallback_reason"] == "client_error"


def test_degraded_response_falls_back() -> None:
    client = FakeClient(decision="review", reason="timeout")

    final, record = _run(client)

    assert final == "review"
    assert record["llm_fallback_reason"] == "timeout"


# ── 传给 AI 的内容 ──────────────────────────────────────────────────────────

def test_payload_carries_criteria_and_metrics_but_not_raw_card_number() -> None:
    """形态判断在平台侧做完，只把**结论**发出去，不发号码。"""
    client = FakeClient(decision="review")

    _run(
        client,
        reasons=["invalid_card_number"],
        quality={"quality_metrics": {}},
        fields={"card_number": "622202020202000"},
    )

    assert client.calls, "边界样本应当触发调用"
    payload = client.calls[0]
    assert payload["boundary_criteria"] == ["near_miss_field_validation"]
    # 断言 finding 是结论而非原始号码
    joined = " ".join(payload["field_findings"])
    assert "15" in joined
    assert "622202020202000" not in joined


def test_quality_metrics_are_forwarded() -> None:
    client = FakeClient(decision="review")

    _run(client, quality={"quality_metrics": {"blur_laplacian_variance": 76.2}})

    assert client.calls[0]["quality_metrics"]["blur_laplacian_variance"] == 76.2


def test_id_card_skips_c3_but_still_uses_c2() -> None:
    """身份证没有格式校验，C3 无判据可用；C2 仍然适用。"""
    client = FakeClient(decision="pass")

    final, record = _run(client, doc_type="id_card")

    assert final == "pass"
    assert record["boundary_criteria"] == ["false_positive_reason_code"]
