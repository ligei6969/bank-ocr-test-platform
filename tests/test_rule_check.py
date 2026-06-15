"""Tests for bank-card rule checks."""

from app.rule_check import is_valid_card_number, is_valid_expiry, review_bank_card


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
