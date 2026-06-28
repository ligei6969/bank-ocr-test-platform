"""Tests for OCR service behavior."""

from app.ocr_service import _extract_text_lines


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
