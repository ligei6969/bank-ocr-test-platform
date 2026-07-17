"""Shared pytest configuration for deterministic OCR mode selection."""

import pytest


@pytest.fixture(autouse=True)
def isolate_ocr_mode(monkeypatch) -> None:
    """Prevent a developer shell OCR_MODE from changing test behavior."""
    monkeypatch.delenv("OCR_MODE", raising=False)
