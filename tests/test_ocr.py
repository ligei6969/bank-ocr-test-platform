"""Tests for OCR service behavior."""

import os
import sys
from types import ModuleType

import pytest

from app.ocr_service import (
    MOCK_OCR_TEXT,
    _configure_paddle_runtime,
    _extract_text_lines,
    _get_paddle_ocr_engine,
    recognize_text,
)


def test_extract_text_lines_from_paddleocr_v3_result() -> None:
    result = [
        {
            "res": {
                "rec_texts": [
                    "TEST BANK",
                    "6222 0202 0202 0001",
                    "VALID THRU 12/30",
                ]
            }
        }
    ]

    assert _extract_text_lines(result) == [
        "TEST BANK",
        "6222 0202 0202 0001",
        "VALID THRU 12/30",
    ]


def test_extract_text_lines_from_paddleocr_v2_result() -> None:
    result = [
        [
            [[[0, 0], [10, 0], [10, 10], [0, 10]], ("TEST BANK", 0.99)],
            [[[0, 20], [10, 20], [10, 30], [0, 30]], ("ZHANG SAN", 0.98)],
        ]
    ]

    assert _extract_text_lines(result) == ["TEST BANK", "ZHANG SAN"]


def test_extract_text_lines_from_nested_objects() -> None:
    class JsonResult:
        def json(self) -> dict:
            return {"data": {"rec_texts": ["  TEST BANK  ", "", "6222"]}}

    class ResResult:
        res = [[("ZHANG SAN", 0.98)]]

    assert _extract_text_lines([None, JsonResult(), ResResult()]) == ["TEST BANK", "6222", "ZHANG SAN"]


def test_extract_text_lines_from_json_attribute() -> None:
    class JsonAttributeResult:
        json = {"res": {"rec_texts": ["ZHOU YI", "07/32"]}}

    assert _extract_text_lines([JsonAttributeResult()]) == ["ZHOU YI", "07/32"]


def test_configure_paddle_runtime_disables_paddlex_mkldnn(monkeypatch) -> None:
    monkeypatch.delenv("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", raising=False)

    _configure_paddle_runtime()

    assert os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] == "False"


def test_recognize_text_defaults_to_mock_without_loading_paddle(monkeypatch) -> None:
    def fail_paddle_ocr(image_path: str) -> list[str]:
        pytest.fail("Paddle OCR should not be used in default mock mode")

    monkeypatch.delitem(sys.modules, "paddleocr", raising=False)
    monkeypatch.setattr("app.ocr_service._recognize_text_with_paddle", fail_paddle_ocr)

    assert recognize_text("bank_card.png") == MOCK_OCR_TEXT
    assert "paddleocr" not in sys.modules


def test_recognize_text_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="Unsupported OCR mode"):
        recognize_text("bank_card.png", mode="unknown")


def test_recognize_text_uses_predict_engine_in_paddle_mode(monkeypatch) -> None:
    class PredictEngine:
        def predict(self, image_path: str) -> list[dict]:
            assert image_path == "bank_card.png"
            return [{"res": {"rec_texts": ["TEST BANK"]}}]

    monkeypatch.setattr("app.ocr_service._get_paddle_ocr_engine", lambda: PredictEngine())

    assert recognize_text("bank_card.png", mode="paddle") == ["TEST BANK"]


def test_recognize_text_uses_legacy_ocr_engine_in_paddle_mode(monkeypatch) -> None:
    class LegacyEngine:
        def ocr(self, image_path: str) -> list:
            assert image_path == "bank_card.png"
            return [[[[0, 0], [1, 0], [1, 1], [0, 1]], ("ZHANG SAN", 0.98)]]

    monkeypatch.setattr("app.ocr_service._get_paddle_ocr_engine", lambda: LegacyEngine())

    assert recognize_text("bank_card.png", mode="paddle") == ["ZHANG SAN"]


def test_paddle_engine_is_cached(monkeypatch) -> None:
    _get_paddle_ocr_engine.cache_clear()
    calls = {"count": 0}

    class FakePaddleOCR:
        def __init__(self, **kwargs) -> None:
            calls["count"] += 1
            assert kwargs == {
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
            }

    fake_module = ModuleType("paddleocr")
    fake_module.PaddleOCR = FakePaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)

    first = _get_paddle_ocr_engine()
    second = _get_paddle_ocr_engine()

    assert first is second
    assert calls["count"] == 1
    _get_paddle_ocr_engine.cache_clear()


def test_paddle_mode_reports_missing_dependency(monkeypatch) -> None:
    _get_paddle_ocr_engine.cache_clear()
    monkeypatch.setitem(sys.modules, "paddleocr", None)

    with pytest.raises(RuntimeError, match="Paddle OCR mode requires PaddleOCR"):
        recognize_text("bank_card.png", mode="paddle")

    _get_paddle_ocr_engine.cache_clear()
