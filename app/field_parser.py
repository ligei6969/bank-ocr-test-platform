"""Field parsing utilities for OCR output."""

from __future__ import annotations

import re
from difflib import SequenceMatcher


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
    raw_lines = [line.strip() for line in ocr_text.splitlines() if line.strip()]
    lines = [_normalize_name_candidate(line) for line in raw_lines]
    excluded: set[int] = set()
    labels: dict[int, int] = {}
    candidates: list[tuple[int, int, str]] = []

    for index, raw in enumerate(raw_lines):
        if _is_expiry_line(raw_lines, index):
            excluded.add(index)
        match = NAME_LABEL_PATTERN.search(raw)
        if match:
            labels[index] = 80
            excluded.add(index)
            value = _normalize_name_candidate(match.group(1))
            if (not re.search(r"\d", match.group(1)) and _is_cardholder_name(value)
                    and not _is_expiry_line([match.group(1), *raw_lines[index + 1:index + 3]], 0)):
                candidates.append((100, -index, value))
        elif _fuzzy_holder_label(lines[index]):
            labels[index] = 70
            excluded.add(index)

    for index, (raw, value) in enumerate(zip(raw_lines, lines)):
        # Digits are evidence of a date/number, not characters to erase into a name.
        if index in excluded or re.search(r"\d", raw) or not _is_cardholder_name(value):
            continue
        # Card number anchors the body; header text is a weaker fallback.
        score = 15 if any(CARD_NUMBER_PATTERN.search(line) for line in raw_lines[:index]) else 10
        for label_index, weight in labels.items():
            distance = index - label_index
            if 1 <= distance <= 3:
                # OCR can interleave an expiry label between holder label and name.
                intervening = range(label_index + 1, index)
                if all(i in excluded for i in intervening):
                    score = max(score, weight - distance)
        candidates.append((score, -index, value))
    return max(candidates)[2] if candidates else None


def _similar_token(value: str, expected: str) -> bool:
    """Bound tolerance by token length; don't fuzzy-match whole arbitrary lines."""
    return abs(len(value) - len(expected)) <= 1 and SequenceMatcher(
        None, value, expected
    ).ratio() >= 1 - 1 / max(len(value), len(expected))


def _fuzzy_holder_label(value: str) -> bool:
    words = value.split()
    return (
        len(words) in (2, 3)
        and words[0] == "CARD"
        and _similar_token(words[1], "HOLDER")
        and (len(words) == 2 or words[2] == "NAME")
    )


def _is_expiry_line(lines: list[str], index: int) -> bool:
    raw = lines[index]
    if DATE_LABEL_PATTERN.match(raw):
        return True
    words = _normalize_name_candidate(raw).split()
    if not words:
        return False
    paired_label = (
        len(words) == 2
        and any(_similar_token(words[0], anchor) for anchor in ("VALID", "GOOD"))
        and any(_similar_token(words[1], anchor) for anchor in ("THRU", "THROUGH", "DATE", "FROM"))
    )
    # A single damaged expiry word needs nearby date evidence. Two matching
    # label tokens already provide enough evidence even when OCR lost the date.
    date_nearby = any(
        VALID_DATE_PATTERN.search(line) or VALID_DATE_COMPACT_PATTERN.search(line)
        for line in lines[max(0, index - 1):index + 3]
        if not CARD_NUMBER_PATTERN.search(line)
    )
    single_label = len(words) == 1 and any(
        _similar_token(words[0], anchor) for anchor in ("EXPIRES", "EXPIRY", "THRU")
    )
    return paired_label or bool(single_label and date_nearby)


def parse_bank_card_fields(ocr_text: str) -> dict[str, str | None]:
    return {
        "card_number": extract_card_number(ocr_text),
        "valid_date": extract_valid_date(ocr_text),
        "name": extract_cardholder_name(ocr_text),
    }
