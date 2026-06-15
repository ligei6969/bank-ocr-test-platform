"""Check unified labels.json statistics and image paths.

Run:
    python scripts/check_labels.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"


def main() -> None:
    labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    doc_type_counts = Counter(str(item.get("doc_type")) for item in labels)
    quality_type_counts = Counter(str(item.get("quality_type")) for item in labels)
    missing = [
        str(item.get("image_path"))
        for item in labels
        if item.get("image_path") and not (ROOT_DIR / str(item.get("image_path"))).exists()
    ]

    print(f"total_labels: {len(labels)}")
    print("doc_type:")
    for key, value in sorted(doc_type_counts.items()):
        print(f"  {key}: {value}")
    print("quality_type:")
    for key, value in sorted(quality_type_counts.items()):
        print(f"  {key}: {value}")
    print(f"missing_images: {len(missing)}")
    if missing:
        print("missing_first_10:")
        for path in missing[:10]:
            print(f"  {path}")


if __name__ == "__main__":
    main()
