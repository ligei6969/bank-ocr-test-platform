"""Generate bank-card abnormal image samples.

Run:
    python scripts/augment_images.py

Only processes data/processed/bank_card/normal/*.png.
"""

from __future__ import annotations

import json
import random
import argparse
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


def add_glare(image: Image.Image, rng: random.Random) -> Image.Image:
    result = image.convert("RGBA")
    width, height = result.size
    long_axis = rng.randint(int(width * 0.36), int(width * 0.48))
    short_axis = rng.randint(int(height * 0.18), int(height * 0.26))
    center_x = rng.randint(int(width * 0.35), int(width * 0.72))
    center_y = rng.randint(int(height * 0.25), int(height * 0.62))
    angle = rng.uniform(-24, 24)

    patch_size = int(max(long_axis, short_axis) * 1.8)
    patch = Image.new("RGBA", (patch_size, patch_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(patch)
    cx = cy = patch_size // 2
    for step in range(12, 0, -1):
        ratio = step / 12
        rx = int(long_axis * ratio / 2)
        ry = int(short_axis * ratio / 2)
        alpha = int(28 + (1 - ratio) * 145)
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(255, 255, 255, alpha))

    core_rx = int(long_axis * 0.23)
    core_ry = int(short_axis * 0.22)
    draw.ellipse((cx - core_rx, cy - core_ry, cx + core_rx, cy + core_ry), fill=(255, 255, 255, 245))
    patch = patch.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    result.alpha_composite(patch, (center_x - patch.size[0] // 2, center_y - patch.size[1] // 2))
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
        return add_glare(image, rng)
    if quality_type == "occlusion":
        return add_occlusion(image)
    if quality_type == "rotate":
        return rotate(image, rng)
    if quality_type == "dark":
        return ImageEnhance.Brightness(image).enhance(0.55)
    if quality_type == "bright":
        return image.point(lambda value: min(255, int(value * 1.25 + 125)))
    raise ValueError(f"Unsupported quality type: {quality_type}")


def normal_bank_labels(labels: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    rows = {}
    for item in labels:
        if item.get("doc_type") == "bank_card" and item.get("quality_type") == "normal":
            rows[str(item["image_path"])] = item
    return rows


def augment(quality_types: tuple[str, ...] = QUALITY_TYPES, seed: int = 20260615) -> list[dict[str, object]]:
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
        for quality_type in quality_types:
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
        if not (item.get("doc_type") == "bank_card" and item.get("quality_type") in quality_types)
    ]
    write_labels(preserved + generated)
    return generated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate bank-card abnormal image samples.")
    parser.add_argument(
        "--types",
        nargs="+",
        choices=QUALITY_TYPES,
        default=list(QUALITY_TYPES),
        help="Quality types to regenerate. Defaults to all abnormal types.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated = augment(tuple(args.types))
    print(f"Generated {len(generated)} augmented bank card images")
    print(f"Wrote labels to {relative(LABELS_PATH)}")


if __name__ == "__main__":
    main()
