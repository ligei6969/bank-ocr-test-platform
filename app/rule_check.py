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

    # 严重退化**先判**，而且必须压过字段缺失。
    #
    # 早先这里把 missing_reasons 放在前面，于是「严重模糊 + 字段读不出」
    # 会返回 review —— 但字段读不出**正是**严重模糊造成的，把它当成
    # 一个独立的 review 理由，等于让症状覆盖了病因。
    #
    # 实测数据支持这个顺序：9 条人工结论为 reject 的样本被平台判成 review，
    # 其中 3 条正是「variance ≈ 1.1（远低于 severe 阈值的 30）+ 字段缺失」。
    # 严重度是**图像自身的性质**，与解析结果无关，所以不受字段层影响。
    severe_reasons = quality.get("severe_reasons") or []
    if severe_reasons:
        # 字段层的非法项一并带上：影像该拒，但「这张卡号本身也不合法」
        # 是审核员该知道的信息
        field_reasons = _field_level_reasons(fields) if not missing_reasons else []
        return "reject", list(severe_reasons) + field_reasons + missing_reasons + quality_reasons

    if missing_reasons:
        return "review", missing_reasons + quality_reasons

    field_reasons = _field_level_reasons(fields)
    if field_reasons:
        return "reject", field_reasons + quality_reasons

    if not is_valid_expiry(str(fields["valid_date"])):
        return "review", ["invalid_valid_date"] + quality_reasons
    if quality_reasons or quality.get("quality_result") == "review":
        return "review", quality_reasons
    return "pass", []


def _field_level_reasons(fields: dict) -> list[str]:
    """字段层面的非法项。缺失的字段不算非法 —— 那是另一类原因码。"""
    card_number = fields.get("card_number")
    if card_number and not is_valid_card_number(str(card_number)):
        return ["invalid_card_number"]
    return []


def review_bank_card(fields: dict, quality: dict) -> str:
    review_result, _review_reasons = review_bank_card_with_reasons(fields, quality)
    return review_result
