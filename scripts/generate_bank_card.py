"""Generate synthetic bank card images and labels.

Run:
    python scripts/generate_bank_card.py

The images are synthetic OCR test assets. They do not use real bank logos,
real card artwork, or real customer data.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT_DIR / "data" / "processed" / "bank_card" / "normal"
LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"
TEMPLATE_PATH = ROOT_DIR / "data" / "templates" / "bank_card" / "test_bank.json"

CARD_SIZE = (760, 460)
DEFAULT_COUNT = 100

NAMES = [
    "ZHANG SAN",
    "LI MING",
    "WANG WEI",
    "ZHAO LEI",
    "CHEN JIE",
    "LIU YANG",
    "SUN QI",
    "ZHOU YI",
    "WU HAO",
    "XU NING",
]


def relative(path: Path) -> str:
    return path.relative_to(ROOT_DIR).as_posix()


def load_template() -> dict[str, object]:
    if TEMPLATE_PATH.exists():
        return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    return {
        "issuer": "TEST BANK",
        "card_type": "Synthetic Debit Card",
        "network": "TestNet",
        "background": ["#29465f", "#516b83"],
        "accent": "#f2c94c",
    }


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


FONTS = {
    "issuer": font(34, bold=True),
    "label": font(18, bold=True),
    "card_number": font(38, bold=True),
    "name": font(24, bold=True),
    "small": font(18),
    "mark": font(22, bold=True),
}


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def blend(start: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    return tuple(int(start[i] + (end[i] - start[i]) * ratio) for i in range(3))


def draw_gradient(draw: ImageDraw.ImageDraw, size: tuple[int, int], start: str, end: str) -> None:
    start_rgb = hex_to_rgb(start)
    end_rgb = hex_to_rgb(end)
    width, height = size
    for y in range(height):
        color = blend(start_rgb, end_rgb, y / max(1, height - 1))
        draw.line((0, y, width, y), fill=color)


def make_card_number(index: int) -> str:
    return f"6222 0202 0202 {index:04d}"


def make_valid_date(rng: random.Random) -> str:
    month = rng.randint(1, 12)
    year = rng.randint(28, 36)
    return f"{month:02d}/{year:02d}"


def draw_chip(draw: ImageDraw.ImageDraw) -> None:
    chip_box = (70, 155, 150, 215)
    draw.rounded_rectangle(chip_box, radius=10, fill=(218, 178, 82), outline=(255, 226, 151), width=2)
    draw.line((95, 155, 95, 215), fill=(140, 112, 50), width=2)
    draw.line((125, 155, 125, 215), fill=(140, 112, 50), width=2)
    draw.line((70, 185, 150, 185), fill=(140, 112, 50), width=2)


def draw_card(fields: dict[str, str], template: dict[str, object], path: Path) -> None:
    image = Image.new("RGB", CARD_SIZE, (20, 35, 52))
    layer = Image.new("RGBA", CARD_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    background = template.get("background", ["#29465f", "#516b83"])
    start, end = str(background[0]), str(background[1])  # type: ignore[index]
    draw_gradient(draw, CARD_SIZE, start, end)
    draw.rounded_rectangle((0, 0, CARD_SIZE[0] - 1, CARD_SIZE[1] - 1), radius=34, outline=(230, 238, 245, 90), width=3)

    accent = hex_to_rgb(str(template.get("accent", "#f2c94c")))
    draw.ellipse((520, -120, 880, 220), fill=(*accent, 55))
    draw.ellipse((-90, 300, 250, 590), fill=(255, 255, 255, 28))
    draw.line((0, 285, 760, 145), fill=(255, 255, 255, 42), width=3)

    draw.text((54, 42), fields["issuer"], font=FONTS["issuer"], fill=(245, 248, 252))
    draw.text((55, 90), "SYNTHETIC CARD", font=FONTS["mark"], fill=(255, 226, 151))
    draw.text((560, 42), "FOR TEST ONLY", font=FONTS["mark"], fill=(255, 226, 151))
    card_type_text = fields["card_type"].upper()
    card_type_bbox = draw.textbbox((0, 0), card_type_text, font=FONTS["small"])
    card_type_width = card_type_bbox[2] - card_type_bbox[0]
    draw.text((CARD_SIZE[0] - card_type_width - 45, 390), card_type_text, font=FONTS["small"], fill=(230, 238, 245))

    draw_chip(draw)
    draw.text((55, 255), fields["card_number"], font=FONTS["card_number"], fill=(248, 250, 252))
    draw.text((55, 330), "CARD HOLDER", font=FONTS["label"], fill=(192, 204, 216))
    draw.text((55, 360), fields["name"], font=FONTS["name"], fill=(248, 250, 252))
    draw.text((335, 330), "VALID THRU", font=FONTS["label"], fill=(192, 204, 216))
    draw.text((335, 360), fields["valid_date"], font=FONTS["name"], fill=(248, 250, 252))
    draw.text((55, 410), "TEST DATA - NOT A REAL PAYMENT CARD", font=FONTS["small"], fill=(215, 226, 235))

    image = Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def read_labels() -> list[dict[str, object]]:
    if LABELS_PATH.exists():
        return json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    return []


def write_labels(labels: list[dict[str, object]]) -> None:
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LABELS_PATH.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")


def generate(count: int, seed: int) -> list[dict[str, object]]:
    rng = random.Random(seed)
    template = load_template()
    issuer = str(template.get("issuer", "TEST BANK"))
    card_type = str(template.get("card_type", "Synthetic Debit Card"))

    bank_labels: list[dict[str, object]] = []
    for index in range(1, count + 1):
        fields = {
            "name": rng.choice(NAMES),
            "card_number": make_card_number(index),
            "valid_date": make_valid_date(rng),
            "issuer": issuer,
            "card_type": card_type,
        }
        image_path = OUTPUT_DIR / f"bank_card_{index:04d}.png"
        draw_card(fields, template, image_path)
        bank_labels.append(
            {
                "image_path": relative(image_path),
                "doc_type": "bank_card",
                "quality_type": "normal",
                "is_synthetic": True,
                "fields": fields,
            }
        )

    labels = [item for item in read_labels() if item.get("doc_type") != "bank_card"]
    write_labels(labels + bank_labels)
    return bank_labels


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synthetic bank card OCR test images.")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=20260615)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    labels = generate(args.count, args.seed)
    print(f"Generated {len(labels)} synthetic bank card images")
    print(f"Wrote labels to {relative(LABELS_PATH)}")


if __name__ == "__main__":
    main()
