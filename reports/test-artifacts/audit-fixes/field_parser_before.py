"""Field parsing utilities for OCR output."""

from __future__ import annotations

import re


CARD_NUMBER_PATTERN = re.compile(r"(?<!\d)(?:\d[\s-]?){16,19}(?!\d)")
VALID_DATE_PATTERN = re.compile(r"\b(0[1-9]|1[0-2])\s*(?:/|-|\.|年)\s*(\d{2,4})\b")
VALID_DATE_COMPACT_PATTERN = re.compile(r"\b(0[1-9]|1[0-2])(\d{2})\b")
NAME_PATTERN = re.compile(r"^[A-Z][A-Z .'-]{1,40}$")


IGNORED_NAME_LINES = {
    "TEST BANK",
    "SYNTHETIC CARD",
    "FOR TEST ONLY",
    "CARD HOLDER",
    "CARDHOLDER",
    "CARDHOLDER NAME",
    "CARD HOLDER NAME",
    "VALID THRU",
    "VALID FROM",
    "EXPIRES",
    "EXPIRY",
    "EXPIRE DATE",
    "VALID DATE",
    "GOOD THRU",
    "THRU",
    "MONTH YEAR",
    "SYNTHETIC DEBIT CARD",
    "TEST DATA",
    "NOT A REAL PAYMENT CARD",
    "DEBIT",
    "CREDIT",
    "UNIONPAY",
    "VISA",
    "MASTERCARD",
}
NAME_STOP_WORDS = {
    "ACCOUNT",
    "BANK",
    "CARD",
    "CREDIT",
    "DATA",
    "DEBIT",
    "OCR",
    "PAYMENT",
    "TEXT",
    "UNRELATED",
}

NAME_LABEL_PATTERN = re.compile(
    r"\b(?:CARD\s*HOLDER\s*NAME|CARDHOLDER\s*NAME|CARD\s*HOLDER|CARDHOLDER|NAME)\b\s*[:：-]?\s*(.*)",
    re.IGNORECASE,
)
DATE_LABEL_PATTERN = re.compile(
    r"\b(?:VALID\s*THRU|VALID\s*DATE|GOOD\s*THRU|EXPIRES?|EXPIRY)\b\s*[:：-]?\s*(.*)",
    re.IGNORECASE,
)
IDENTITY_LABEL_PATTERN = re.compile(r"(身份证|身份号码|公民身份号码|ID\s*NO|IDENTITY)", re.IGNORECASE)


def normalize_card_number(value: str) -> str | None:
    """Return a normalized 16-19 digit card number, or None when invalid."""
    digits = re.sub(r"\D", "", value)
    if not 16 <= len(digits) <= 19:
        return None
    return digits


def extract_card_number(ocr_text: str) -> str | None:
    for line in ocr_text.splitlines():
        if IDENTITY_LABEL_PATTERN.search(line):
            continue
        for match in CARD_NUMBER_PATTERN.finditer(line):
            card_number = normalize_card_number(match.group(0))
            if card_number:
                return card_number
    return None


def _normalize_valid_date(month: str, year: str) -> str | None:
    if len(year) == 4:
        year = year[-2:]
    return f"{month}/{year}"


def extract_valid_date(ocr_text: str) -> str | None:
    for line in ocr_text.splitlines():
        label_match = DATE_LABEL_PATTERN.search(line)
        if label_match:
            labeled_value = label_match.group(1)
            match = VALID_DATE_PATTERN.search(labeled_value) or VALID_DATE_COMPACT_PATTERN.search(labeled_value)
            if match:
                return _normalize_valid_date(match.group(1), match.group(2))

    for line in ocr_text.splitlines():
        if CARD_NUMBER_PATTERN.search(line):
            continue
        match = VALID_DATE_PATTERN.search(line) or VALID_DATE_COMPACT_PATTERN.search(line)
        if match:
            return _normalize_valid_date(match.group(1), match.group(2))
    return None


def _normalize_name_candidate(value: str) -> str:
    value = re.sub(r"[^A-Z .'-]", " ", value.upper())
    return " ".join(value.split()).strip(" .'-")


def _is_cardholder_name(value: str) -> bool:
    if not NAME_PATTERN.fullmatch(value):
        return False
    if value in IGNORED_NAME_LINES:
        return False
    if any(char.isdigit() for char in value):
        return False
    words = value.split()
    if len(words) < 2:
        return False
    if any(word in NAME_STOP_WORDS for word in words):
        return False
    return all(any(char.isalpha() for char in word) for word in words)


def extract_cardholder_name(ocr_text: str) -> str | None:
    lines = [_normalize_name_candidate(line) for line in ocr_text.splitlines()]

    for index, raw_line in enumerate(ocr_text.splitlines()):
        label_match = NAME_LABEL_PATTERN.search(raw_line)
        if not label_match:
            continue
        labeled_value = _normalize_name_candidate(label_match.group(1))
        if _is_cardholder_name(labeled_value):
            return labeled_value
        if index + 1 < len(lines) and _is_cardholder_name(lines[index + 1]):
            return lines[index + 1]

    for value in lines:
        if _is_cardholder_name(value):
            return value
    return None


def parse_bank_card_fields(ocr_text: str) -> dict[str, str | None]:
    return {
        "card_number": extract_card_number(ocr_text),
        "valid_date": extract_valid_date(ocr_text),
        "name": extract_cardholder_name(ocr_text),
    }
