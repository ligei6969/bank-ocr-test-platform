"""Augment synthetic ID-card normal images into abnormal quality samples.

Run:
    python scripts/augment_id_card_images.py
"""

from __future__ import annotations

import json
import random
from copy import deepcopy
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter


ROOT_DIR = Path(__file__).resolve().parents[1]
ID_CARD_DIR = ROOT_DIR / "data" / "processed" / "id_card"
LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"
QUALITY_TYPES = ("blur", "glare", "occlusion", "rotate", "dark", "bright")
SIDES = ("front", "back")


def relative(path: Path) -> str:
    return path.relative_to(ROOT_DIR).as_posix()


def load_labels() -> list[dict[str, object]]:
    if not LABELS_PATH.exists():
        return []
    return json.loads(LABELS_PATH.read_text(encoding="utf-8"))


def write_labels(labels: list[dict[str, object]]) -> None:
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LABELS_PATH.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")


def add_glare(image: Image.Image, rng: random.Random) -> Image.Image:
    result = image.convert("RGBA")
    overlay = Image.new("RGBA", result.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = result.size
    cx = rng.randint(int(w * 0.50), int(w * 0.85))
    cy = rng.randint(int(h * 0.05), int(h * 0.35))
    rx = rng.randint(int(w * 0.18), int(w * 0.32))
    ry = rng.randint(int(h * 0.12), int(h * 0.24))
    draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(255, 255, 255, 104))
    x0 = rng.randint(int(w * 0.08), int(w * 0.28))
    draw.polygon(
        [(x0, 0), (x0 + int(w * 0.12), 0), (x0 + int(w * 0.72), h), (x0 + int(w * 0.55), h)],
        fill=(255, 255, 255, 54),
    )
    result.alpha_composite(overlay)
    return result.convert("RGB")


def add_occlusion(image: Image.Image, rng: random.Random) -> Image.Image:
    result = image.copy()
    draw = ImageDraw.Draw(result)
    w, h = result.size
    box_w = rng.randint(int(w * 0.18), int(w * 0.34))
    box_h = rng.randint(int(h * 0.08), int(h * 0.16))
    x0 = rng.randint(int(w * 0.48), max(int(w * 0.49), w - box_w - 30))
    y0 = rng.randint(int(h * 0.22), max(int(h * 0.23), h - box_h - 45))
    draw.rounded_rectangle((x0, y0, x0 + box_w, y0 + box_h), radius=8, fill=(35, 41, 48))
    return result


def rotate_image(image: Image.Image, rng: random.Random) -> Image.Image:
    angle = rng.uniform(-8, 8)
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=(222, 226, 221))


def augment_image(image: Image.Image, quality_type: str, rng: random.Random) -> Image.Image:
    if quality_type == "blur":
        return image.filter(ImageFilter.GaussianBlur(radius=2.1))
    if quality_type == "glare":
        return add_glare(image, rng)
    if quality_type == "occlusion":
        return add_occlusion(image, rng)
    if quality_type == "rotate":
        return rotate_image(image, rng)
    if quality_type == "dark":
        return ImageEnhance.Brightness(image).enhance(0.55)
    if quality_type == "bright":
        return ImageEnhance.Brightness(image).enhance(1.45)
    raise ValueError(f"Unsupported quality type: {quality_type}")


def normal_label_map(labels: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for item in labels:
        if item.get("doc_type") in {"id_card_front", "id_card_back"} and item.get("quality_type") == "normal":
            path = item.get("image_path")
            if isinstance(path, str):
                rows[path] = item
    return rows


def clear_augmented_images() -> None:
    for side in SIDES:
        for quality_type in QUALITY_TYPES:
            output_dir = ID_CARD_DIR / side / quality_type
            if not output_dir.exists():
                continue
            for path in output_dir.glob("id_*.jpg"):
                path.unlink()


def augment(seed: int = 20260615) -> list[dict[str, object]]:
    rng = random.Random(seed)
    labels = load_labels()
    source_labels = normal_label_map(labels)
    generated: list[dict[str, object]] = []
    clear_augmented_images()

    for side in SIDES:
        normal_dir = ID_CARD_DIR / side / "normal"
        for source_path in sorted(normal_dir.glob("*.jpg")):
            source_rel = relative(source_path)
            source_label = source_labels.get(source_rel)
            if not source_label:
                continue

            with Image.open(source_path) as image:
                base = image.convert("RGB")
            for quality_type in QUALITY_TYPES:
                output_dir = ID_CARD_DIR / side / quality_type
                output_path = output_dir / source_path.name
                output_dir.mkdir(parents=True, exist_ok=True)
                augment_image(base, quality_type, rng).save(output_path, format="JPEG", quality=95, optimize=True)

                new_label = deepcopy(source_label)
                new_label["image_path"] = relative(output_path)
                new_label["quality_type"] = quality_type
                new_label["source"] = "augmented_from_normal"
                generated.append(new_label)

    preserved = [
        item
        for item in labels
        if not (
            item.get("doc_type") in {"id_card_front", "id_card_back"}
            and item.get("quality_type") in QUALITY_TYPES
        )
    ]
    write_labels(preserved + generated)
    return generated


def main() -> None:
    generated = augment()
    print(f"Generated {len(generated)} augmented ID-card images")
    print(f"Wrote labels to {relative(LABELS_PATH)}")


if __name__ == "__main__":
    main()
