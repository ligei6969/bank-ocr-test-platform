"""Logging helpers for masking sensitive document numbers."""

from __future__ import annotations

import re
from typing import Any


ID_NUMBER_PATTERN = re.compile(r"(?<!\d)(\d{6})(\d{8})(\d{3}[\dXx])(?!\d)")
CARD_NUMBER_PATTERN = re.compile(r"(?<!\d)(?:\d[\s-]?){15,18}\d(?!\d)")


def _mask_card_number(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return f"{digits[:6]}{'*' * (len(digits) - 10)}{digits[-4:]}"


def mask_sensitive_data(value: str) -> str:
    """Mask bank-card and Chinese ID numbers in arbitrary log text."""
    masked = ID_NUMBER_PATTERN.sub(
        lambda match: f"{match.group(1)}{'*' * len(match.group(2))}{match.group(3)}",
        value,
    )
    return CARD_NUMBER_PATTERN.sub(_mask_card_number, masked)


def sanitize_for_log(value: Any) -> Any:
    """Recursively mask sensitive strings before passing values to a logger."""
    if isinstance(value, str):
        return mask_sensitive_data(value)
    if isinstance(value, dict):
        return {key: sanitize_for_log(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_log(item) for item in value)
    return value


# Text fields that carry personal data but cannot be handled by the numeric
# patterns above. Names and addresses are not digit strings, so the regex based
# masking silently leaves them intact — which is exactly the leak that matters
# when a payload leaves the process boundary.
REDACT_TEXT_FIELDS = frozenset(
    {
        "name",
        "holder",
        "holder_name",
        "card_holder",
        "account_name",
        "customer_name",
        "address",
        "home_address",
        "姓名",
        "持卡人",
        "户名",
        "客户姓名",
        "住址",
        "地址",
    }
)


def mask_text_keep_prefix(value: str) -> str:
    """Keep the first character and star the rest.

    Used for personal-data text such as names and addresses where the numeric
    patterns do not apply. A single-character value is masked completely so a
    one-character name is never echoed back in full.
    """
    text = value.strip()
    if not text:
        return ""
    if len(text) == 1:
        return "*"
    return f"{text[0]}{'*' * (len(text) - 1)}"


def sanitize_review_fields(fields: Any) -> Any:
    """Mask a parsed-field mapping before it leaves the process boundary.

    Numeric values go through :func:`sanitize_for_log`; values whose key is in
    :data:`REDACT_TEXT_FIELDS` are additionally reduced to a single character.
    """
    masked = sanitize_for_log(fields)
    if not isinstance(fields, dict) or not isinstance(masked, dict):
        return masked

    for key, raw in fields.items():
        if not isinstance(raw, str):
            continue
        if str(key).strip().lower() in REDACT_TEXT_FIELDS:
            masked[key] = mask_text_keep_prefix(raw)
    return masked
    