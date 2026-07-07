"""Evaluate bank-card OCR output against synthetic labels."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.field_parser import parse_bank_card_fields  # noqa: E402
from app.ocr_service import OCRMode, recognize_text  # noqa: E402


LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"
OUTPUT_PATH = ROOT_DIR / "reports" / "bank_card_ocr_evaluation.csv"
CSV_FIELDS = [
    "image_path",
    "expected_card_number",
    "predicted_card_number",
    "card_number_correct",
    "expected_name",
    "predicted_name",
    "name_correct",
    "expected_valid_date",
    "predicted_valid_date",
    "valid_date_correct",
    "error_message",
]


def normalize_card_number(value: Any) -> str:
    return "".join(str(value or "").split())


def normalize_name(value: Any) -> str:
    return str(value or "").strip().upper()


def normalize_valid_date(value: Any) -> str:
    return str(value or "").strip()


def load_samples(labels_path: Path = LABELS_PATH, limit: int = 10) -> list[dict[str, Any]]:
    with labels_path.open("r", encoding="utf-8") as file:
        labels = json.load(file)

    samples = [
        item
        for item in labels
        if item.get("doc_type") == "bank_card" and item.get("quality_type") == "normal"
    ]
    return samples[:limit]


def _expected_fields(sample: dict[str, Any]) -> dict[str, str]:
    fields = sample.get("fields") or {}
    return {
        "card_number": normalize_card_number(fields.get("card_number")),
        "name": normalize_name(fields.get("name")),
        "valid_date": normalize_valid_date(fields.get("valid_date")),
    }


def _empty_prediction() -> dict[str, str]:
    return {"card_number": "", "name": "", "valid_date": ""}


def evaluate_sample(sample: dict[str, Any], mode: OCRMode) -> dict[str, Any]:
    image_path = str(sample.get("image_path", ""))
    expected = _expected_fields(sample)
    predicted = _empty_prediction()
    error_message = ""

    try:
        resolved_image_path = ROOT_DIR / image_path
        ocr_text = recognize_text(str(resolved_image_path), mode=mode)
        parsed = parse_bank_card_fields("\n".join(ocr_text))
        predicted = {
            "card_number": normalize_card_number(parsed.get("card_number")),
            "name": normalize_name(parsed.get("name")),
            "valid_date": normalize_valid_date(parsed.get("valid_date")),
        }
    except Exception as exc:  # noqa: BLE001 - per-sample failures must be reported and skipped.
        error_message = str(exc)

    return {
        "image_path": image_path,
        "expected_card_number": expected["card_number"],
        "predicted_card_number": predicted["card_number"],
        "card_number_correct": predicted["card_number"] == expected["card_number"] and not error_message,
        "expected_name": expected["name"],
        "predicted_name": predicted["name"],
        "name_correct": predicted["name"] == expected["name"] and not error_message,
        "expected_valid_date": expected["valid_date"],
        "predicted_valid_date": predicted["valid_date"],
        "valid_date_correct": predicted["valid_date"] == expected["valid_date"] and not error_message,
        "error_message": error_message,
    }


def evaluate_samples(samples: list[dict[str, Any]], mode: OCRMode) -> list[dict[str, Any]]:
    return [evaluate_sample(sample, mode) for sample in samples]


def write_csv(rows: list[dict[str, Any]], output_path: Path = OUTPUT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    total = len(rows)
    success_rows = [row for row in rows if not row["error_message"]]
    success_count = len(success_rows)
    failed_count = total - success_count

    def accuracy(field: str) -> float:
        if not success_rows:
            return 0.0
        correct = sum(1 for row in success_rows if row[field])
        return correct / success_count

    full_correct = sum(
        1
        for row in success_rows
        if row["card_number_correct"] and row["name_correct"] and row["valid_date_correct"]
    )
    return {
        "total": total,
        "success": success_count,
        "failed": failed_count,
        "card_number_accuracy": accuracy("card_number_correct"),
        "name_accuracy": accuracy("name_correct"),
        "valid_date_accuracy": accuracy("valid_date_correct"),
        "full_field_accuracy": full_correct / success_count if success_rows else 0.0,
    }


def print_summary(summary: dict[str, float | int]) -> None:
    print(f"Total samples: {summary['total']}")
    print(f"Successful inference: {summary['success']}")
    print(f"Failed inference: {summary['failed']}")
    print(f"card_number accuracy: {summary['card_number_accuracy']:.2%}")
    print(f"name accuracy: {summary['name_accuracy']:.2%}")
    print(f"valid_date accuracy: {summary['valid_date_accuracy']:.2%}")
    print(f"Full-field exact accuracy: {summary['full_field_accuracy']:.2%}")
    print(f"CSV saved to: {OUTPUT_PATH}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate bank-card OCR against normal synthetic labels.")
    parser.add_argument("--mode", choices=["mock", "paddle"], default="mock")
    parser.add_argument("--limit", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    samples = load_samples(limit=args.limit)
    rows = evaluate_samples(samples, mode=args.mode)
    write_csv(rows)
    print_summary(summarize(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
