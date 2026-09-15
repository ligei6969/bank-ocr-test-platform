"""Tests for the explanation layer.

The important property under test is the fact/wording split: reason-code facts
(thresholds, implementation locations, advice) must be correct even when no LLM
is configured. Degradation may only cost wording quality, never accuracy.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ai_service.explain import (
    DISCLAIMER,
    ReviewContext,
    build_explainer,
    classify_reason_code,
    split_labeled_sections,
)
from ai_service.llm import NullLLMClient
from ai_service.prompts import EXPLAIN_GENERATE, QUERY_REWRITE, RERANK


class FakeLLM:
    """Deterministic stand-in for a real provider."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:fake:test-model"

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        self.prompts.append(prompt)
        if "改写" in prompt:
            return '["为什么需要人工复核", "image_blur 是什么意思", "该怎么让用户重拍"]'
        if "排序" in prompt:
            return "[0, 1, 2, 3, 4]"
        return "这是由模型生成的解释正文，说明根因在影像质量层。"


def blurred_card_context() -> ReviewContext:
    return ReviewContext(
        request_id="req-001",
        doc_type="bank_card",
        review_result="review",
        quality_result="review",
        quality_reasons=["image_blur"],
        review_reasons=["missing_valid_date", "image_blur"],
        fields={"card_number": "622202******7890", "name": "张*"},
        question="这张卡为什么需要人工复核？",
    )


def explain(context: ReviewContext, llm: Any = None, top_k: int = 5) -> dict[str, Any]:
    explainer = build_explainer(llm=llm or NullLLMClient())
    return asyncio.run(explainer.explain(context, top_k=top_k))


# ── 标签切分 ──────────────────────────────────────────────────────────────────

def test_split_labeled_sections_extracts_labels_and_lead() -> None:
    lead, sections = split_labeled_sections(
        "原因码 image_blur，含义是图片模糊。触发条件：清晰度过低。处置建议：让用户重拍。"
    )

    assert lead.startswith("原因码 image_blur")
    assert sections["触发条件"] == "清晰度过低。"
    assert sections["处置建议"] == "让用户重拍。"


def test_split_labeled_sections_handles_unlabelled_text() -> None:
    lead, sections = split_labeled_sections("这是一段没有任何标签的说明。")

    assert lead == "这是一段没有任何标签的说明。"
    assert sections == {}


# ── 上下文 ────────────────────────────────────────────────────────────────────

def test_reason_codes_are_merged_and_deduplicated_in_order() -> None:
    context = ReviewContext(
        request_id="x",
        quality_reasons=["image_blur", "glare_detected"],
        review_reasons=["missing_valid_date", "image_blur"],
    )

    assert context.all_reason_codes == ["missing_valid_date", "image_blur", "glare_detected"]


def test_missing_question_falls_back_to_a_default() -> None:
    assert ReviewContext(request_id="x").effective_question


def test_from_payload_tolerates_missing_and_mistyped_fields() -> None:
    context = ReviewContext.from_payload({"request_id": "x", "quality_reasons": "not-a-list"})

    assert context.quality_reasons == []
    assert context.fields == {}
    assert context.doc_type == "bank_card"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("image_blur", "quality"),
        ("glare_detected", "quality"),
        ("missing_card_number", "field"),
        ("invalid_card_number", "field"),
        ("internal_error", "infrastructure"),
        ("invalid_ocr_mode", "infrastructure"),
        ("something_new", "field"),
    ],
)
def test_reason_code_classification(code: str, expected: str) -> None:
    assert classify_reason_code(code) == expected


# ── 无 LLM 路径（CI 主路径）──────────────────────────────────────────────────

def test_explain_without_llm_returns_the_full_payload_shape() -> None:
    result = explain(blurred_card_context())

    for key in (
        "request_id",
        "doc_type",
        "review_result",
        "explanation",
        "reason_details",
        "actions",
        "citations",
        "unknown_reason_codes",
        "confidence",
        "degraded",
        "engine",
        "latency_ms",
        "trace",
        "disclaimer",
    ):
        assert key in result, key

    assert result["request_id"] == "req-001"
    assert result["disclaimer"] == DISCLAIMER


def test_reason_details_stay_factually_correct_without_an_llm() -> None:
    """降级只损失措辞，不损失事实 —— 这是本设计的核心承诺。"""
    result = explain(blurred_card_context())
    details = {item["code"]: item for item in result["reason_details"]}

    assert set(details) == {"missing_valid_date", "image_blur"}
    blur = details["image_blur"]
    assert blur["known"] is True
    assert blur["root_cause"] == "quality"
    # 阈值来自平台真实实现，必须原样出现
    assert "80.0" in blur["implementation"]
    assert "app/quality_check.py" in blur["implementation"]
    assert blur["advice"]
    assert blur["user_message"]


def test_actions_include_the_cross_cutting_root_cause_judgement() -> None:
    result = explain(blurred_card_context())
    joined = " ".join(result["actions"])

    # 质量 + 字段同时出现时，应先修质量而不是追问用户字段
    assert "先修影像质量" in joined
    assert "不必单独追问用户字段内容" in joined


def test_explanation_mentions_root_cause_layers_without_an_llm() -> None:
    result = explain(blurred_card_context())

    assert "待人工复核" in result["explanation"]
    assert "影像质量层" in result["explanation"]
    assert "missing_valid_date" in result["explanation"]


def test_engine_reports_the_degraded_strategies() -> None:
    result = explain(blurred_card_context())

    assert result["degraded"] is True
    assert result["engine"]["generation"] == "template"
    assert result["engine"]["rewrite"] == "rule"
    assert result["engine"]["rerank"] == "consensus"
    assert result["engine"]["llm_available"] is False
    assert result["engine"]["recall"] == "ok"


def test_trace_records_every_stage_of_the_chain() -> None:
    steps = [entry["step"] for entry in explain(blurred_card_context())["trace"]]

    assert steps[0] == "query_built"
    assert "rewrite" in steps
    assert "merge" in steps
    assert "rerank" in steps


def test_citations_carry_retrieved_knowledge() -> None:
    result = explain(blurred_card_context())

    assert result["citations"]
    top = result["citations"][0]
    assert top["doc_id"]
    assert top["title"]
    assert top["snippet"]
    assert top["content"]


def test_infrastructure_codes_forbid_asking_the_user_to_reshoot() -> None:
    result = explain(
        ReviewContext(
            request_id="req-002",
            review_result="error",
            review_reasons=["internal_error"],
        )
    )

    assert "不要" in result["actions"][0]
    assert "重拍提示" in result["actions"][0]


def test_glare_only_review_is_flagged_as_a_likely_false_positive() -> None:
    result = explain(
        ReviewContext(
            request_id="req-003",
            review_result="review",
            quality_result="review",
            quality_reasons=["glare_detected"],
            review_reasons=["glare_detected"],
            fields={"card_number": "622202******7890", "valid_date": "08/29", "name": "张*"},
        )
    )

    joined = " ".join(result["actions"])
    assert "放行" in joined
    assert "误报样本" in joined


def test_card_number_reject_with_quality_issue_prefers_misread_over_forgery() -> None:
    result = explain(
        ReviewContext(
            request_id="req-004",
            review_result="reject",
            quality_result="review",
            quality_reasons=["image_blur"],
            review_reasons=["invalid_card_number", "image_blur"],
        )
    )

    joined = " ".join(result["actions"])
    assert "优先怀疑 OCR 误识" in joined


def test_unknown_reason_codes_are_surfaced_instead_of_silently_dropped() -> None:
    result = explain(
        ReviewContext(
            request_id="req-005",
            review_result="review",
            review_reasons=["brand_new_code"],
        )
    )

    assert result["unknown_reason_codes"] == ["brand_new_code"]
    detail = result["reason_details"][0]
    assert detail["known"] is False
    assert detail["root_cause"] == "unknown"
    assert "语料" in detail["meaning"]


def test_record_without_reason_codes_says_nothing_needs_doing() -> None:
    result = explain(
        ReviewContext(request_id="req-006", review_result="pass", quality_result="pass")
    )

    assert result["reason_details"] == []
    assert "没有审核原因码" in result["explanation"]
    assert result["actions"]


def test_confidence_is_bounded_and_discounted_without_an_llm() -> None:
    degraded = explain(blurred_card_context())
    with_llm = explain(blurred_card_context(), llm=FakeLLM())

    for result in (degraded, with_llm):
        assert 0.0 <= result["confidence"] <= 1.0
    assert degraded["confidence"] < with_llm["confidence"]


def test_top_k_limits_citations() -> None:
    result = explain(blurred_card_context(), top_k=2)

    assert len(result["citations"]) <= 2


# ── LLM 路径 ──────────────────────────────────────────────────────────────────

def test_explain_uses_the_llm_for_rewrite_rerank_and_wording() -> None:
    llm = FakeLLM()
    result = explain(blurred_card_context(), llm=llm)

    assert result["degraded"] is False
    assert result["engine"]["generation"] == "llm"
    assert result["engine"]["rewrite"] == "llm"
    assert result["engine"]["rerank"] == "llm"
    assert result["explanation"] == "这是由模型生成的解释正文，说明根因在影像质量层。"
    assert len(llm.prompts) >= 3


def test_llm_path_still_returns_corpus_facts_unchanged() -> None:
    """LLM 只负责措辞，事实条目必须与无 LLM 路径逐字一致。"""
    degraded = explain(blurred_card_context())
    with_llm = explain(blurred_card_context(), llm=FakeLLM())

    assert with_llm["reason_details"] == degraded["reason_details"]
    assert with_llm["actions"] == degraded["actions"]


# ── Prompt 版本回传 ───────────────────────────────────────────────────────────

def test_degraded_path_reports_no_prompt_versions() -> None:
    """没调模型就不能报 prompt 版本 —— 报了会让人误以为模型参与了生成。"""
    result = explain(blurred_card_context())

    assert result["prompt_versions"]["used"] == {}
    assert result["prompt_versions"]["registry"]


def test_llm_path_reports_the_prompt_versions_it_used() -> None:
    result = explain(blurred_card_context(), llm=FakeLLM())

    used = result["prompt_versions"]["used"]
    assert used["query_rewrite"] == QUERY_REWRITE.label
    assert used["rerank"] == RERANK.label
    assert used["explain_generate"] == EXPLAIN_GENERATE.label


def test_prompt_version_labels_are_reachable_from_the_trace() -> None:
    """trace 是排查的第一手材料，prompt 版本必须能在里面直接看到。"""
    result = explain(blurred_card_context(), llm=FakeLLM())

    steps = {entry["step"]: entry for entry in result["trace"]}
    assert steps["rewrite"]["prompt"] == QUERY_REWRITE.label
    assert steps["generate"]["prompt"] == EXPLAIN_GENERATE.label
