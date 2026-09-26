"""Tests for bank-card rule checks."""

import pytest

from app.rule_check import (
    is_valid_card_number,
    is_valid_expiry,
    review_bank_card,
    review_bank_card_with_reasons,
)


def test_valid_card_number_accepts_16_to_19_digits() -> None:
    assert is_valid_card_number("6222020202020001")
    assert is_valid_card_number("6222020202020001001")


def test_invalid_card_number_rejects_non_digits_or_bad_length() -> None:
    assert not is_valid_card_number("6222 0202 0202 0001")
    assert not is_valid_card_number("123")
    assert not is_valid_card_number("6222020202020001X")


def test_valid_expiry() -> None:
    assert is_valid_expiry("12/30")
    assert not is_valid_expiry("13/30")
    assert not is_valid_expiry("1230")


def test_review_when_required_fields_missing() -> None:
    result = review_bank_card(
        {"card_number": "6222020202020001", "valid_date": "12/30"},
        {"is_blur": False, "brightness": "normal", "has_glare": False},
    )

    assert result == "review"


def test_reject_when_card_number_invalid() -> None:
    result = review_bank_card(
        {"card_number": "123", "name": "ZHANG SAN", "valid_date": "12/30"},
        {"is_blur": False, "brightness": "normal", "has_glare": False},
    )

    assert result == "reject"


def test_review_when_expiry_is_invalid() -> None:
    result = review_bank_card(
        {"card_number": "6222020202020001", "name": "ZHANG SAN", "valid_date": "13/30"},
        {"is_blur": False, "brightness": "normal", "has_glare": False},
    )

    assert result == "review"


def test_review_when_quality_has_problem() -> None:
    fields = {"card_number": "6222020202020001", "name": "ZHANG SAN", "valid_date": "12/30"}

    assert review_bank_card(fields, {"is_blur": True, "brightness": "normal", "has_glare": False}) == "review"
    assert review_bank_card(fields, {"is_blur": False, "brightness": "dark", "has_glare": False}) == "review"
    assert review_bank_card(fields, {"is_blur": False, "brightness": "bright", "has_glare": False}) == "review"
    assert review_bank_card(fields, {"is_blur": False, "brightness": "normal", "has_glare": True}) == "review"


def test_pass_when_fields_and_quality_are_valid() -> None:
    result = review_bank_card(
        {"card_number": "6222020202020001", "name": "ZHANG SAN", "valid_date": "12/30"},
        {"is_blur": False, "brightness": "normal", "has_glare": False},
    )

    assert result == "pass"


def test_missing_fields_return_specific_review_reasons() -> None:
    result, reasons = review_bank_card_with_reasons(
        {"name": "ZHANG SAN"},
        {"is_blur": False, "brightness": "normal", "has_glare": False},
    )

    assert result == "review"
    assert reasons == ["missing_card_number", "missing_valid_date"]


def test_invalid_card_number_returns_reject_reason() -> None:
    result, reasons = review_bank_card_with_reasons(
        {"card_number": "123", "name": "ZHANG SAN", "valid_date": "12/30"},
        {"is_blur": False, "brightness": "normal", "has_glare": False},
    )

    assert result == "reject"
    assert reasons == ["invalid_card_number"]


def test_quality_reason_is_included_in_review_reasons() -> None:
    result, reasons = review_bank_card_with_reasons(
        {"card_number": "6222020202020001", "name": "ZHANG SAN", "valid_date": "12/30"},
        {"is_blur": True, "brightness": "normal", "has_glare": False},
    )

    assert result == "review"
    assert reasons == ["image_blur"]


# ── 严重退化 → reject ─────────────────────────────────────────────────────────

VALID_FIELDS = {"card_number": "6222020202020001", "name": "ZHANG SAN", "valid_date": "12/30"}


@pytest.mark.parametrize(
    "severe_reason",
    ["severe_image_blur", "severe_image_dark", "severe_image_bright", "severe_glare_detected"],
)
def test_reject_when_degradation_is_severe(severe_reason: str) -> None:
    """严重退化重拍之外无补救手段，结论是 reject 而不是转人工。"""
    quality = {
        "is_blur": False,
        "brightness": "normal",
        "has_glare": False,
        "quality_result": "review",
        "quality_reasons": ["image_blur"],
        "severe_reasons": [severe_reason],
    }

    result, reasons = review_bank_card_with_reasons(VALID_FIELDS, quality)

    assert result == "reject"
    assert severe_reason in reasons


def test_missing_fields_still_outrank_severity() -> None:
    """字段缺失应先给出具体缺失项，严重度判拒也要带上原因码。"""
    quality = {
        "is_blur": False,
        "brightness": "normal",
        "has_glare": False,
        "severe_reasons": ["severe_image_blur"],
    }

    result, reasons = review_bank_card_with_reasons({}, quality)

    assert result == "review"
    assert "missing_card_number" in reasons


def test_severity_does_not_override_invalid_card_number() -> None:
    """卡号本身非法时，reject 的原因码要保留 invalid_card_number。"""
    quality = {
        "is_blur": False,
        "brightness": "normal",
        "has_glare": False,
        "severe_reasons": ["severe_image_dark"],
    }

    result, reasons = review_bank_card_with_reasons(
        {"card_number": "123", "name": "ZHANG SAN", "valid_date": "12/30"},
        quality,
    )

    assert result == "reject"
    assert "invalid_card_number" in reasons
    assert "severe_image_dark" in reasons


def test_quality_without_severe_key_is_unaffected() -> None:
    """没有 severe_reasons 键的判定（各处 mock 的老形状）行为必须不变。"""
    result, reasons = review_bank_card_with_reasons(
        VALID_FIELDS,
        {"is_blur": True, "brightness": "normal", "has_glare": False},
    )

    assert result == "review"
    assert reasons == ["image_blur"]
