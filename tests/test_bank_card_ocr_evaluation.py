"""Tests for the bank-card OCR evaluation script."""

from scripts import evaluate_bank_card_ocr


def test_evaluation_normalizes_fields() -> None:
    assert evaluate_bank_card_ocr.normalize_card_number("6222 0202 0202 0001") == "6222020202020001"
    assert evaluate_bank_card_ocr.normalize_name(" zhang san ") == "ZHANG SAN"
    assert evaluate_bank_card_ocr.normalize_valid_date("07/32") == "07/32"


def test_evaluation_continues_after_single_sample_failure(monkeypatch) -> None:
    samples = [
        {
            "image_path": "data/processed/bank_card/normal/bank_card_0001.png",
            "fields": {
                "card_number": "6222 0202 0202 0001",
                "name": "ZHOU YI",
                "valid_date": "07/32",
            },
        },
        {
            "image_path": "data/processed/bank_card/normal/bank_card_0002.png",
            "fields": {
                "card_number": "6222 0202 0202 0002",
                "name": "ZHANG SAN",
                "valid_date": "08/32",
            },
        },
    ]

    def fake_recognize_text(image_path: str, mode: str = "mock") -> list[str]:
        if image_path.endswith("bank_card_0001.png"):
            raise RuntimeError("fake OCR failure")
        return [
            "TEST BANK",
            "6222 0202 0202 0002",
            "CARD HOLDER",
            "ZHANG SAN",
            "VALID THRU 08/32",
        ]

    monkeypatch.setattr(evaluate_bank_card_ocr, "recognize_text", fake_recognize_text)

    rows = evaluate_bank_card_ocr.evaluate_samples(samples, mode="mock")

    assert len(rows) == 2
    assert rows[0]["error_message"] == "fake OCR failure"
    assert rows[0]["card_number_correct"] is False
    assert rows[1]["error_message"] == ""
    assert rows[1]["card_number_correct"] is True
    assert rows[1]["name_correct"] is True
    assert rows[1]["valid_date_correct"] is True
