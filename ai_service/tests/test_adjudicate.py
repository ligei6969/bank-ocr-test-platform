"""Tests for the dual-judge adjudication surface.

重点是**安全不变式**：模型只能维持 review 或降为 pass，任何失败都回落规则原判。
这些性质靠结构保证（``Literal`` + 业务层兜底），所以要用测试把它们钉住 ——
否则将来有人放宽 schema 时不会有任何东西报警。
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_service.adjudication import (
    REASON_NO_MODEL,
    REASON_UNPARSEABLE,
    AdjudicationVerdict,
    adjudicate,
)
from ai_service.agentkit import BrokenPlanner, ProsePlanner, ScriptedPlanner
from ai_service.api import create_app
from ai_service.llm import LLMUnavailableError, NullLLMClient


def _verdict_llm(decision: str = "pass", **extra: Any) -> ScriptedPlanner:
    payload = {
        "decision": decision,
        "confidence": extra.pop("confidence", 0.8),
        "rationale": extra.pop("rationale", "反光位于镭射区，关键字段完整"),
        "risk_notes": extra.pop("risk_notes", []),
    }
    return ScriptedPlanner([payload])


async def _call(llm, **overrides) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "llm": llm,
        "request_id": "req-1",
        "doc_type": "bank_card",
        "review_result": "review",
        "review_reasons": ["glare_detected"],
        "boundary_criteria": ["false_positive_reason_code"],
        "quality_metrics": {"glare_component_ratio": 0.0066},
    }
    kwargs.update(overrides)
    return await adjudicate(**kwargs)


# ── 安全不变式：模型无法加重结论 ────────────────────────────────────────────

@pytest.mark.parametrize("illegal", ["reject", "REJECT", "pass ", "", "escalate"])
def test_verdict_schema_rejects_anything_but_review_or_pass(illegal: str) -> None:
    """这是本设计最重要的一条不变式：模型在**结构上**产不出 reject。

    靠的是 pydantic 的 Literal，不是 prompt 里的措辞劝阻 ——
    措辞在对抗输入下没有约束力，schema 有。
    """
    with pytest.raises(ValidationError):
        AdjudicationVerdict(decision=illegal)


def test_model_cannot_escalate_even_if_it_tries() -> None:
    """模型输出 reject 时整条复核视为失败，回落规则原判 —— 绝不加重。"""
    llm = ScriptedPlanner([{"decision": "reject", "confidence": 0.9, "rationale": "假卡"}])

    import asyncio

    result = asyncio.run(_call(llm))

    assert result["decision"] == "review"      # 规则原判
    assert result["overrode"] is False
    assert result["degraded"] is True
    assert result["reason"] == REASON_UNPARSEABLE


# ── 正常路径 ────────────────────────────────────────────────────────────────

def test_model_pass_produces_override() -> None:
    import asyncio

    result = asyncio.run(_call(_verdict_llm("pass")))

    assert result["decision"] == "pass"
    assert result["overrode"] is True
    assert result["degraded"] is False
    assert result["rule_decision"] == "review"
    assert result["rationale"]


def test_model_review_keeps_rule_verdict() -> None:
    import asyncio

    result = asyncio.run(_call(_verdict_llm("review")))

    assert result["decision"] == "review"
    assert result["overrode"] is False
    assert result["degraded"] is False


def test_prompt_carries_the_evidence_the_model_needs() -> None:
    """模型要看到原始指标与判据，否则只能瞎猜。"""
    import asyncio

    llm = _verdict_llm("pass")
    asyncio.run(_call(llm, quality_metrics={"blur_laplacian_variance": 76.2}))

    prompt = llm.prompts[0]
    assert "76.2" in prompt
    assert "false_positive_reason_code" in prompt
    assert "glare_detected" in prompt


# ── 失败一律回落 ────────────────────────────────────────────────────────────

def test_model_unavailable_falls_back_to_rule_verdict() -> None:
    import asyncio

    result = asyncio.run(_call(NullLLMClient()))

    assert result["decision"] == "review"
    assert result["reason"] == REASON_NO_MODEL
    assert result["degraded"] is True


def test_unparseable_output_falls_back() -> None:
    import asyncio

    result = asyncio.run(_call(ProsePlanner()))

    assert result["decision"] == "review"
    assert result["reason"] == REASON_UNPARSEABLE


def test_model_exception_falls_back() -> None:
    import asyncio

    result = asyncio.run(_call(BrokenPlanner(LLMUnavailableError("boom"))))

    assert result["decision"] == "review"
    assert result["degraded"] is True
    assert result["overrode"] is False


@pytest.mark.parametrize("result_value", ["reject", "pass", "error", ""])
def test_non_review_never_calls_the_model(result_value: str) -> None:
    """pass/reject/error 不该被复核 —— 这是成本控制，也是权限边界。"""
    import asyncio

    llm = _verdict_llm("pass")
    out = asyncio.run(_call(llm, review_result=result_value))

    assert out["decision"] == result_value        # 原样返回，未被改动
    assert out["overrode"] is False
    assert llm.calls == 0                         # 模型一次都没被调用


# ── HTTP 端点 ───────────────────────────────────────────────────────────────

def test_endpoint_returns_200_and_degrades_without_a_model() -> None:
    """离线默认（NullLLMClient）必须 200 + degraded，而不是 500。"""
    client = TestClient(create_app())

    response = client.post(
        "/adjudicate",
        json={
            "request_id": "req-1",
            "doc_type": "bank_card",
            "review_result": "review",
            "review_reasons": ["glare_detected"],
            "boundary_criteria": ["false_positive_reason_code"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "review"
    assert body["overrode"] is False
    assert body["degraded"] is True


def test_endpoint_rejects_unknown_doc_type() -> None:
    client = TestClient(create_app())

    response = client.post("/adjudicate", json={"request_id": "r", "doc_type": "passport"})

    assert response.status_code == 422


def test_endpoint_reports_the_metric_surface() -> None:
    """新 surface 必须注册进 SURFACES，否则指标会被静默归到 other。"""
    from ai_service.metrics import SURFACES

    assert "adjudicate" in SURFACES
