"""Business rule checks for parsed OCR fields."""

from __future__ import annotations

import re

from app.quality_check import get_quality_reasons


REQUIRED_BANK_CARD_FIELDS = ["card_number", "valid_date", "name"]


def is_valid_card_number(card_number: str) -> bool:
    return bool(re.fullmatch(r"\d{16,19}", card_number or ""))


def is_valid_expiry(valid_date: str) -> bool:
    return bool(re.fullmatch(r"(0[1-9]|1[0-2])/\d{2}", valid_date or ""))


def review_bank_card_with_reasons(fields: dict, quality: dict) -> tuple[str, list[str]]:
    missing_reasons = [
        f"missing_{field}"
        for field in REQUIRED_BANK_CARD_FIELDS
        if not fields.get(field)
    ]
    quality_reasons = get_quality_reasons(quality)
    if missing_reasons:
        return "review", missing_reasons + quality_reasons

    card_number = str(fields["card_number"])
    valid_date = str(fields["valid_date"])
    if not is_valid_card_number(card_number):
        return "reject", ["invalid_card_number"] + quality_reasons
    if not is_valid_expiry(valid_date):
        return "review", ["invalid_valid_date"] + quality_reasons
    if quality_reasons or quality.get("quality_result") == "review":
        return "review", quality_reasons
    return "pass", []


def review_bank_card(fields: dict, quality: dict) -> str:
    review_result, _review_reasons = review_bank_card_with_reasons(fields, quality)
    return review_result
