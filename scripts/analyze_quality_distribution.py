"""Analyze quality metric distributions for processed bank-card images."""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any

import cv2
import numpy as np


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.quality_check import check_image_quality  # noqa: E402


BANK_CARD_DATA_DIR = ROOT_DIR / "data" / "processed" / "bank_card"
REPORT_PATH = ROOT_DIR / "reports" / "quality_distribution.csv"
QUALITY_TYPES = ("normal", "blur", "glare", "occlusion", "rotate", "dark", "bright")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def iter_image_paths(quality_type: str) -> list[Path]:
    image_dir = BANK_CARD_DATA_DIR / quality_type
    return sorted(path for path in image_dir.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES)


def load_image(image_path: Path) -> np.ndarray:
    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError(f"Unable to read image: {image_path}")
    return image


def highlight_mask(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    value = hsv[:, :, 2]
    saturation = hsv[:, :, 1]
    return (value > 245) & (saturation < 45)


def largest_component_ratio(mask: np.ndarray) -> float:
    component_count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask.astype("uint8"), 8)
    if component_count <= 1:
        return 0.0
    largest_area = int(stats[1:, cv2.CC_STAT_AREA].max())
    return float(largest_area / mask.size)


def calculate_metrics(image_path: Path, quality_type: str) -> dict[str, Any]:
    image = load_image(image_path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mask = highlight_mask(image)
    quality = check_image_quality(str(image_path))

    return {
        "image_path": str(image_path.relative_to(ROOT_DIR)),
        "quality_type": quality_type,
        "grayscale_mean": float(gray.mean()),
        "laplacian_variance": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "glare_pixel_ratio": float(mask.mean()),
        "largest_highlight_component_ratio": largest_component_ratio(mask),
        "is_blur": quality["is_blur"],
        "brightness": quality["brightness"],
        "has_glare": quality["has_glare"],
        "quality_result": quality["quality_result"],
    }


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "min": min(values),
        "mean": mean(values),
        "median": median(values),
        "max": max(values),
    }


def format_summary(values: list[float]) -> str:
    stats = summarize(values)
    return (
        f"min={stats['min']:.4f}, mean={stats['mean']:.4f}, "
        f"median={stats['median']:.4f}, max={stats['max']:.4f}"
    )


def write_csv(rows: list[dict[str, Any]]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "image_path",
        "quality_type",
        "grayscale_mean",
        "laplacian_variance",
        "glare_pixel_ratio",
        "largest_highlight_component_ratio",
        "is_blur",
        "brightness",
        "has_glare",
        "quality_result",
    ]
    with REPORT_PATH.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_metric_summary(rows_by_type: dict[str, list[dict[str, Any]]]) -> None:
    metrics = (
        "grayscale_mean",
        "laplacian_variance",
        "glare_pixel_ratio",
        "largest_highlight_component_ratio",
    )
    print("Metric distribution by quality_type")
    for quality_type in QUALITY_TYPES:
        rows = rows_by_type[quality_type]
        print(f"\n[{quality_type}] samples={len(rows)}")
        for metric in metrics:
            values = [float(row[metric]) for row in rows]
            print(f"  {metric}: {format_summary(values)}")


def print_detector_summary(rows_by_type: dict[str, list[dict[str, Any]]]) -> None:
    detector_checks = {
        "normal pass ratio": ("normal", lambda row: row["quality_result"] == "pass"),
        "blur detection ratio": ("blur", lambda row: row["is_blur"] is True),
        "dark detection ratio": ("dark", lambda row: row["brightness"] == "dark"),
        "bright detection ratio": ("bright", lambda row: row["brightness"] == "bright"),
        "glare detection ratio": ("glare", lambda row: row["has_glare"] is True),
    }
    print("\nCurrent detector result ratios")
    for label, (quality_type, predicate) in detector_checks.items():
        rows = rows_by_type[quality_type]
        matched = sum(1 for row in rows if predicate(row))
        ratio = matched / len(rows) if rows else 0.0
        print(f"  {label}: {matched}/{len(rows)} = {ratio:.2%}")


def main() -> None:
    rows: list[dict[str, Any]] = []
    rows_by_type: dict[str, list[dict[str, Any]]] = {}

    for quality_type in QUALITY_TYPES:
        type_rows = [calculate_metrics(path, quality_type) for path in iter_image_paths(quality_type)]
        rows_by_type[quality_type] = type_rows
        rows.extend(type_rows)

    write_csv(rows)
    print(f"Wrote {len(rows)} rows to {REPORT_PATH.relative_to(ROOT_DIR)}")
    print_metric_summary(rows_by_type)
    print_detector_summary(rows_by_type)


if __name__ == "__main__":
    main()
