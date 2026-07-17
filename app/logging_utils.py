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
