"""Business rule checks for parsed OCR fields."""

from __future__ import annotations

import re


def is_valid_card_number(card_number: str) -> bool:
    return bool(re.fullmatch(r"\d{16,19}", card_number or ""))


def is_valid_expiry(valid_date: str) -> bool:
    return bool(re.fullmatch(r"(0[1-9]|1[0-2])/\d{2}", valid_date or ""))


def review_bank_card(fields: dict, quality: dict) -> str:
    card_number = fields.get("card_number")
    name = fields.get("name")
    valid_date = fields.get("valid_date")

    if not card_number or not name or not valid_date:
        return "review"
    if not is_valid_card_number(str(card_number)):
        return "reject"
    if not is_valid_expiry(str(valid_date)):
        return "review"

    if quality.get("is_blur"):
        return "review"
    if quality.get("brightness") in {"dark", "bright"}:
        return "review"
    if quality.get("has_glare"):
        return "review"

    return "pass"
