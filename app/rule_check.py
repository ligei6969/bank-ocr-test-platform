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
    # 字段层面的非法项先算出来：即便影像严重退化，也一并告诉审核员
    # 「这张卡的卡号本身也不合法」，否则原因码会漏掉已能确定的信息
    field_reasons = [] if is_valid_card_number(card_number) else ["invalid_card_number"]

    severe_reasons = quality.get("severe_reasons") or []
    if severe_reasons:
        return "reject", list(severe_reasons) + field_reasons + quality_reasons
    if field_reasons:
        return "reject", field_reasons + quality_reasons
    if not is_valid_expiry(valid_date):
        return "review", ["invalid_valid_date"] + quality_reasons
    if quality_reasons or quality.get("quality_result") == "review":
        return "review", quality_reasons
    return "pass", []


def review_bank_card(fields: dict, quality: dict) -> str:
    review_result, _review_reasons = review_bank_card_with_reasons(fields, quality)
    return review_result
