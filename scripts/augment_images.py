"""Generate bank-card abnormal image samples.

Run:
    python scripts/augment_images.py

Only processes data/processed/bank_card/normal/*.png.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter


ROOT_DIR = Path(__file__).resolve().parents[1]
BANK_CARD_DIR = ROOT_DIR / "data" / "processed" / "bank_card"
NORMAL_DIR = BANK_CARD_DIR / "normal"
LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"
QUALITY_TYPES = ("blur", "glare", "occlusion", "rotate", "dark", "bright")


def relative(path: Path) -> str:
    return path.relative_to(ROOT_DIR).as_posix()


def read_labels() -> list[dict[str, object]]:
    if LABELS_PATH.exists():
        return json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    return []


def write_labels(labels: list[dict[str, object]]) -> None:
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LABELS_PATH.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")


def add_glare(image: Image.Image) -> Image.Image:
    result = image.convert("RGBA")
    overlay = Image.new("RGBA", result.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.ellipse((390, 35, 810, 250), fill=(255, 255, 255, 95))
    draw.polygon([(125, 0), (230, 0), (720, 460), (590, 460)], fill=(255, 255, 255, 48))
    result.alpha_composite(overlay)
    return result.convert("RGB")


def add_occlusion(image: Image.Image) -> Image.Image:
    result = image.copy()
    draw = ImageDraw.Draw(result)
    draw.rounded_rectangle((300, 252, 560, 305), radius=6, fill=(44, 48, 54))
    return result


def rotate(image: Image.Image, rng: random.Random) -> Image.Image:
    angle = rng.uniform(-8, 8)
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=(20, 35, 52))


def augment_image(image: Image.Image, quality_type: str, rng: random.Random) -> Image.Image:
    if quality_type == "blur":
        return image.filter(ImageFilter.GaussianBlur(radius=2.4))
    if quality_type == "glare":
        return add_glare(image)
    if quality_type == "occlusion":
        return add_occlusion(image)
    if quality_type == "rotate":
        return rotate(image, rng)
    if quality_type == "dark":
        return ImageEnhance.Brightness(image).enhance(0.55)
    if quality_type == "bright":
        return ImageEnhance.Brightness(image).enhance(1.45)
    raise ValueError(f"Unsupported quality type: {quality_type}")


def normal_bank_labels(labels: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    rows = {}
    for item in labels:
        if item.get("doc_type") == "bank_card" and item.get("quality_type") == "normal":
            rows[str(item["image_path"])] = item
    return rows


def augment(seed: int = 20260615) -> list[dict[str, object]]:
    rng = random.Random(seed)
    labels = read_labels()
    normal_labels = normal_bank_labels(labels)
    generated: list[dict[str, object]] = []

    for source_path in sorted(NORMAL_DIR.glob("*.png")):
        source_rel = relative(source_path)
        source_label = normal_labels.get(source_rel)
        if not source_label:
            continue
        image = Image.open(source_path).convert("RGB")
        for quality_type in QUALITY_TYPES:
            output_dir = BANK_CARD_DIR / quality_type
            output_path = output_dir / source_path.name
            output_dir.mkdir(parents=True, exist_ok=True)
            augment_image(image, quality_type, rng).save(output_path)

            new_label = dict(source_label)
            new_label["image_path"] = relative(output_path)
            new_label["quality_type"] = quality_type
            new_label["fields"] = dict(source_label["fields"])  # type: ignore[index]
            generated.append(new_label)

    preserved = [
        item
        for item in labels
        if not (item.get("doc_type") == "bank_card" and item.get("quality_type") in QUALITY_TYPES)
    ]
    write_labels(preserved + generated)
    return generated


def main() -> None:
    generated = augment()
    print(f"Generated {len(generated)} augmented bank card images")
    print(f"Wrote labels to {relative(LABELS_PATH)}")


if __name__ == "__main__":
    main()
