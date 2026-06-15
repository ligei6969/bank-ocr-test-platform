"""Field parsing utilities for OCR output."""

from __future__ import annotations

import re


CARD_NUMBER_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){16}(?!\d)")
VALID_DATE_PATTERN = re.compile(r"\b(0[1-9]|1[0-2])\s*/\s*(\d{2})\b")
NAME_PATTERN = re.compile(r"^[A-Z]{2,}\s+[A-Z]{2,}$")


def normalize_card_number(value: str) -> str | None:
    """Return a 16-digit synthetic card number, or None when invalid."""
    digits = re.sub(r"\D", "", value)
    if len(digits) != 16:
        return None
    if not digits.startswith("6222"):
        return None
    return digits


def extract_card_number(ocr_text: str) -> str | None:
    for match in CARD_NUMBER_PATTERN.finditer(ocr_text):
        card_number = normalize_card_number(match.group(0))
        if card_number:
            return card_number
    return None


def extract_valid_date(ocr_text: str) -> str | None:
    match = VALID_DATE_PATTERN.search(ocr_text)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def extract_cardholder_name(ocr_text: str) -> str | None:
    ignored = {
        "TEST BANK",
        "SYNTHETIC CARD",
        "FOR TEST ONLY",
        "CARD HOLDER",
        "VALID THRU",
        "SYNTHETIC DEBIT CARD",
        "TEST DATA",
        "NOT A REAL PAYMENT CARD",
    }
    for line in ocr_text.upper().splitlines():
        value = " ".join(line.split())
        if not NAME_PATTERN.fullmatch(value):
            continue
        if value not in ignored:
            return value
    return None


def parse_bank_card_fields(ocr_text: str) -> dict[str, str | None]:
    return {
        "card_number": extract_card_number(ocr_text),
        "valid_date": extract_valid_date(ocr_text),
        "name": extract_cardholder_name(ocr_text),
    }
