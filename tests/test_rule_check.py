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


def test_severity_outranks_missing_fields_but_keeps_them_in_reasons() -> None:
    """严重退化压过字段缺失，但缺失项仍要出现在原因码里。

    **这条测试改过。** 原版叫 ``test_missing_fields_still_outrank_severity``，
    断言 ``result == "review"`` —— 而它的 docstring 写的是
    「严重度判拒也要带上原因码」。两者矛盾：实现里 ``missing_reasons``
    在 ``severe_reasons`` 之前返回，于是 ``severe_image_blur``
    **被整条丢掉**，返回的是 ``review``。测试只检查了
    ``missing_card_number`` 在不在，没检查严重度是否幸存，
    所以这个缺陷一直是绿的。

    CTE-3 用真实数据发现了它：9 条人工结论为 ``reject`` 的样本被平台判成
    ``review``，其中 3 条正是「variance ≈ 1.1（远低于 severe 阈值 30）+
    字段缺失」—— 字段读不出**正是**严重模糊造成的，让症状覆盖病因是错的。
    """
    quality = {
        "is_blur": False,
        "brightness": "normal",
        "has_glare": False,
        "severe_reasons": ["severe_image_blur"],
    }

    result, reasons = review_bank_card_with_reasons({}, quality)

    assert result == "reject", "影像严重到该拒，不该因为字段缺失降级成 review"
    assert "severe_image_blur" in reasons, "严重度必须出现在原因码里（原实现把它丢了）"
    assert "missing_card_number" in reasons, "缺失项仍要告诉审核员"


def test_severity_with_unreadable_fields_is_rejected_not_reviewed() -> None:
    """回归 CTE-3 抓到的真实场景：严重模糊图上字段全读不出。

    对应快照样本 ``bank_card/blur/bank_card_0001.png``
    （variance 1.08，severe 阈值 30）。
    """
    quality = {
        "is_blur": True,
        "brightness": "normal",
        "has_glare": False,
        "quality_result": "review",
        "quality_reasons": ["image_blur"],
        "quality_metrics": {"blur_laplacian_variance": 1.08, "brightness_mean": 80.6, "glare_component_ratio": 0.0},
        "severe_reasons": ["severe_image_blur"],
    }

    result, reasons = review_bank_card_with_reasons(
        {"card_number": None, "valid_date": None, "name": None}, quality
    )

    assert result == "reject"
    assert reasons[0] == "severe_image_blur", "严重度应排在原因码最前"


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
