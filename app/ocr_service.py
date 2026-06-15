"""OCR service integration layer."""

from __future__ import annotations


def recognize_text(image_path: str) -> list[str]:
    """Return fixed OCR-like text for the bank-card review flow.

    This is intentionally a mock implementation. The project can later replace
    it with PaddleOCR without changing the review endpoint contract.
    """
    return [
        "TEST BANK",
        "SYNTHETIC CARD",
        "6222 0202 0202 0001",
        "CARD HOLDER",
        "ZHANG SAN",
        "VALID THRU 12/30",
        "FOR TEST ONLY",
    ]
