"""Generate marked synthetic bank-card images and JSON annotations.

The generated cards are fictional OCR test fixtures. They do not copy real
card artwork, real bank logos, or real card numbers. The local Cardentify
sample is used only as a size/layout reference when present.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "synthetic" / "bank_card" / "Synthetic Test Bank"
DEFAULT_LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"
REFERENCE_CARD_PATH = (
    ROOT_DIR
    / "Cardentify-main"
    / "Cardentify-main"
    / "Cards"
    / "Bank of China"
    / "上海哔哩哔哩联名借记卡.png"
)

FALLBACK_CARD_SIZE = (1536, 969)
RENDER_SCALE = 2
QUALITY_TYPES = ("normal", "blur", "glare", "occlusion", "rotate")
WATERMARK = "SYNTHETIC CARD / FOR TEST ONLY"
BANK_NAME = "SYNTHETIC TEST BANK"
NATIVE_BANK_NAME = "合成测试银行"
CARD_BRAND = "TestNet"

INK = (30, 39, 48)
MUTED = (104, 115, 126)
WHITE = (255, 255, 255)
PINK = (237, 166, 183)
TEAL = (76, 160, 163)
GREEN = (72, 128, 98)
GOLD = (204, 163, 80)
RED = (196, 52, 64)

GIVEN_NAMES = [
    "ALEX CHEN",
    "MAYA LI",
    "JORDAN WANG",
    "NINA ZHOU",
    "ERIC LIN",
    "TINA XU",
    "SAM SUN",
    "IVY QIN",
]


def resolve_project_path(path: Path) -> Path:
    return path if path.is_absolute() else ROOT_DIR / path


def relative(path: Path) -> str:
    return path.relative_to(ROOT_DIR).as_posix()


def resolve_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def build_fonts(scale: int) -> dict[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    return {
        "bank": resolve_font(46 * scale, bold=True),
        "meta": resolve_font(32 * scale, bold=True),
        "number": resolve_font(56 * scale, bold=True),
        "label": resolve_font(21 * scale, bold=True),
        "value": resolve_font(31 * scale, bold=True),
        "small": resolve_font(24 * scale, bold=True),
        "brand": resolve_font(42 * scale, bold=True),
        "watermark": resolve_font(34 * scale, bold=True),
        "micro": resolve_font(18 * scale, bold=True),
    }


FONTS = build_fonts(1)


def card_size(reference_path: Path) -> tuple[int, int]:
    if reference_path.exists():
        with Image.open(reference_path) as image:
            return image.size
    return FALLBACK_CARD_SIZE


def spoint(x: int, y: int, scale: int) -> tuple[int, int]:
    return (x * scale, y * scale)


def sbox(box: tuple[int, int, int, int], scale: int) -> tuple[int, int, int, int]:
    return tuple(value * scale for value in box)  # type: ignore[return-value]


def luhn_check_digit(prefix: str) -> str:
    total = 0
    reverse_digits = list(map(int, reversed(prefix + "0")))
    for idx, digit in enumerate(reverse_digits):
        if idx % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - total % 10) % 10)


def make_card_number(rng: random.Random, index: int) -> str:
    prefix = f"900000{index:04d}{rng.randint(0, 99999):05d}"
    return prefix + luhn_check_digit(prefix)


def grouped_card_number(card_number: str) -> str:
    return " ".join(card_number[idx : idx + 4] for idx in range(0, len(card_number), 4))


def random_fields(rng: random.Random, index: int) -> dict[str, str]:
    expire_year = 2031 + (index % 5)
    expire_month = 1 + (index * 7) % 12
    return {
        "bank_name": BANK_NAME,
        "card_product": "Synthetic Debit Card",
        "card_number": make_card_number(rng, index),
        "name": rng.choice(GIVEN_NAMES),
        "expire_date": f"{expire_month:02d}/{str(expire_year)[-2:]}",
        "issuer_bin": "900000",
        "network": CARD_BRAND,
    }


def draw_background(draw: ImageDraw.ImageDraw, size: tuple[int, int], rng: random.Random) -> None:
    width, height = size
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(216 + 22 * ratio)
        g = int(239 - 18 * ratio)
        b = int(236 + 2 * ratio)
        draw.line((0, y, width, y), fill=(r, g, b))

    trunk = [
        (int(width * 0.23), 0),
        (int(width * 0.34), 0),
        (int(width * 0.42), height),
        (int(width * 0.28), height),
    ]
    draw.polygon(trunk, fill=(104, 129, 82))
    draw.line((int(width * 0.34), 0, int(width * 0.61), int(height * 0.38)), fill=(91, 116, 76), width=14)
    draw.line((int(width * 0.24), int(height * 0.13), int(width * 0.03), int(height * 0.40)), fill=(91, 116, 76), width=10)

    for _ in range(480):
        x = rng.randint(0, width)
        y = rng.randint(0, int(height * 0.55))
        radius = rng.randint(2, 7)
        color = rng.choice([(245, 185, 206), (250, 211, 225), (238, 151, 183), (247, 238, 244)])
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)

    for _ in range(54):
        x = rng.randint(int(width * 0.02), int(width * 0.98))
        y = rng.randint(int(height * 0.12), int(height * 0.92))
        length = rng.randint(25, 80)
        draw.ellipse((x, y, x + length, y + length // 4), fill=(251, 184, 204), outline=None)

    for _ in range(36):
        x = rng.randint(0, width)
        y = rng.randint(int(height * 0.62), height)
        draw.line((x, y, x + rng.randint(-60, 80), y - rng.randint(80, 180)), fill=(71, 132, 96), width=rng.randint(3, 7))


def draw_chip(draw: ImageDraw.ImageDraw, x: int, y: int, scale: int) -> None:
    box = sbox((x, y, x + 118, y + 88), scale)
    draw.rounded_rectangle(box, radius=12 * scale, fill=(217, 185, 111), outline=(128, 103, 57), width=2 * scale)
    draw.line((box[0], box[1] + 31 * scale, box[2], box[1] + 31 * scale), fill=(128, 103, 57), width=2 * scale)
    draw.line((box[0], box[1] + 58 * scale, box[2], box[1] + 58 * scale), fill=(128, 103, 57), width=2 * scale)
    draw.line((box[0] + 39 * scale, box[1], box[0] + 39 * scale, box[3]), fill=(128, 103, 57), width=2 * scale)
    draw.line((box[0] + 78 * scale, box[1], box[0] + 78 * scale, box[3]), fill=(128, 103, 57), width=2 * scale)


def draw_contactless(draw: ImageDraw.ImageDraw, x: int, y: int, scale: int) -> None:
    for idx in range(4):
        offset = idx * 18 * scale
        draw.arc(
            (x * scale + offset, y * scale - 56 * scale, x * scale + (94 + idx * 18) * scale, y * scale + 82 * scale),
            start=-42,
            end=42,
            fill=(119, 129, 136),
            width=8 * scale,
        )


def draw_test_network(draw: ImageDraw.ImageDraw, width: int, height: int, scale: int) -> None:
    x0 = width - 385 * scale
    y0 = height - 205 * scale
    draw.rounded_rectangle(
        (x0, y0, width - 55 * scale, height - 60 * scale),
        radius=28 * scale,
        fill=(126, 134, 139, 174),
    )
    draw.text((x0 + 34 * scale, y0 + 39 * scale), "TEST", font=FONTS["brand"], fill=WHITE)
    draw.text((x0 + 160 * scale, y0 + 43 * scale), "NET", font=FONTS["brand"], fill=WHITE)
    draw.line((x0 + 32 * scale, y0 + 103 * scale, width - 91 * scale, y0 + 103 * scale), fill=(236, 236, 236), width=3 * scale)


def draw_watermark(image: Image.Image, scale: int) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    text = WATERMARK
    bbox = draw.textbbox((0, 0), text, font=FONTS["watermark"])
    x = (image.size[0] - (bbox[2] - bbox[0])) // 2
    y = (image.size[1] - (bbox[3] - bbox[1])) // 2
    draw.rounded_rectangle(
        (x - 28 * scale, y - 18 * scale, x + (bbox[2] - bbox[0]) + 28 * scale, y + (bbox[3] - bbox[1]) + 22 * scale),
        radius=14 * scale,
        fill=(255, 255, 255, 76),
    )
    draw.text((x, y), text, font=FONTS["watermark"], fill=(194, 35, 50, 142))
    rotated = layer.rotate(-14, resample=Image.Resampling.BICUBIC, center=(image.size[0] // 2, image.size[1] // 2))
    image.alpha_composite(rotated)


def render_card(fields: dict[str, str], seed: int, target_size: tuple[int, int]) -> Image.Image:
    global FONTS
    scale = RENDER_SCALE
    FONTS = build_fonts(scale)
    size = (target_size[0] * scale, target_size[1] * scale)
    rng = random.Random(seed)
    image = Image.new("RGBA", size, (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)

    draw_background(draw, size, rng)
    margin = 24 * scale
    draw.rounded_rectangle(
        (margin, margin, size[0] - margin, size[1] - margin),
        radius=42 * scale,
        outline=(255, 255, 255, 150),
        width=4 * scale,
    )
    draw.rectangle((0, 0, size[0], int(size[1] * 0.18)), fill=(255, 255, 255, 58))

    draw.rounded_rectangle(
        sbox((62, 75, 154, 148), scale),
        radius=12 * scale,
        outline=(96, 106, 112, 185),
        width=5 * scale,
    )
    draw.text(spoint(78, 91, scale), "STB", font=FONTS["small"], fill=(86, 96, 104, 190))
    draw.text(spoint(178, 72, scale), fields["bank_name"], font=FONTS["bank"], fill=(86, 96, 104, 190))
    draw.text(spoint(515, 116, scale), "DEBIT", font=FONTS["meta"], fill=(86, 96, 104, 185))
    draw.text(spoint(1180, 92, scale), "test only", font=FONTS["meta"], fill=(86, 96, 104, 150))

    draw_chip(draw, 91, 266, scale)
    draw_contactless(draw, 1160, 365, scale)
    draw.text(spoint(92, 520, scale), grouped_card_number(fields["card_number"]), font=FONTS["number"], fill=INK)
    draw.text(spoint(93, 658, scale), "CARDHOLDER", font=FONTS["label"], fill=MUTED)
    draw.text(spoint(93, 694, scale), fields["name"], font=FONTS["value"], fill=INK)
    draw.text(spoint(490, 658, scale), "VALID THRU", font=FONTS["label"], fill=MUTED)
    draw.text(spoint(490, 694, scale), fields["expire_date"], font=FONTS["value"], fill=INK)
    draw.text(spoint(93, 772, scale), "SYNTHETIC TEST DATA", font=FONTS["small"], fill=(170, 39, 52))
    draw.text(spoint(93, 820, scale), "NOT ISSUED BY ANY REAL BANK", font=FONTS["small"], fill=(170, 39, 52))
    draw_test_network(draw, size[0], size[1], scale)

    for idx in range(18):
        x = (920 + idx * 34) * scale
        y = (450 + int(math.sin(idx * 0.7) * 44)) * scale
        draw.line((x, y, x + 58 * scale, y - 74 * scale), fill=(255, 255, 255, 110), width=3 * scale)

    draw_watermark(image, scale)
    return image.convert("RGB").resize(target_size, resample=Image.Resampling.LANCZOS)


def add_glare(image: Image.Image) -> Image.Image:
    result = image.convert("RGBA")
    overlay = Image.new("RGBA", result.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = result.size
    draw.ellipse((width * 0.48, -height * 0.06, width * 1.12, height * 0.73), fill=(255, 255, 255, 110))
    draw.polygon(
        [(width * 0.05, 0), (width * 0.18, 0), (width * 0.86, height), (width * 0.72, height)],
        fill=(255, 255, 255, 58),
    )
    result.alpha_composite(overlay)
    return result.convert("RGB")


def add_occlusion(image: Image.Image) -> Image.Image:
    result = image.copy()
    draw = ImageDraw.Draw(result)
    width, height = result.size
    font = resolve_font(max(22, width // 52), bold=True)
    draw.rounded_rectangle(
        (int(width * 0.36), int(height * 0.52), int(width * 0.77), int(height * 0.63)),
        radius=max(8, width // 110),
        fill=(36, 42, 48),
    )
    draw.text((int(width * 0.40), int(height * 0.55)), "OCR TEST MASK", font=font, fill=(245, 245, 245))
    return result


def rotate_image(image: Image.Image) -> Image.Image:
    return image.rotate(5, resample=Image.Resampling.BICUBIC, fillcolor=(226, 239, 236))


def apply_quality(image: Image.Image, quality_type: str) -> Image.Image:
    if quality_type == "normal":
        return image
    if quality_type == "blur":
        return image.filter(ImageFilter.GaussianBlur(radius=2.0))
    if quality_type == "glare":
        return add_glare(image)
    if quality_type == "occlusion":
        return add_occlusion(image)
    if quality_type == "rotate":
        return rotate_image(image)
    raise ValueError(f"Unsupported quality type: {quality_type}")


def image_filename(index: int) -> str:
    return f"synthetic_debit_{index:03d}.png"


def save_variant(base_image: Image.Image, output_dir: Path, index: int, quality_type: str) -> Path:
    path = output_dir / quality_type / image_filename(index)
    path.parent.mkdir(parents=True, exist_ok=True)
    apply_quality(base_image, quality_type).save(path)
    return path


def build_catalog(records: list[dict[str, object]], output_dir: Path) -> dict[str, object]:
    cards = []
    for record in records:
        fields = record["fields"]
        assert isinstance(fields, dict)
        cards.append(
            {
                "description": fields["card_product"],
                "bin": [int(fields["issuer_bin"])],
                "card": {
                    "type": "Debit",
                    "brand": CARD_BRAND,
                    "country": "TEST",
                    "level": "Synthetic",
                },
                "source": "Generated by scripts/generate_synthetic_bank_cards.py",
                "ext": "png",
                "image_path": record["normal_image_path"],
                "synthetic": True,
                "warning": WATERMARK,
            }
        )
    return {
        "bank": {
            "native_name": NATIVE_BANK_NAME,
            "english_name": BANK_NAME,
            "country": "TEST",
            "url": None,
            "synthetic": True,
        },
        "cards": cards,
        "notes": [
            "Fictional OCR test fixtures.",
            "No real bank logo, real card artwork, or real cardholder data is used.",
        ],
        "catalog_dir": relative(output_dir),
    }


def load_existing_labels(labels_path: Path) -> list[dict[str, object]]:
    if not labels_path.exists():
        return []
    return json.loads(labels_path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def generate(count: int, output_dir: Path, labels_path: Path, seed: int, start_index: int) -> list[dict[str, object]]:
    output_dir = resolve_project_path(output_dir)
    labels_path = resolve_project_path(labels_path)
    rng = random.Random(seed)
    target_size = card_size(REFERENCE_CARD_PATH)
    labels: list[dict[str, object]] = []
    catalog_records: list[dict[str, object]] = []

    for index in range(start_index, start_index + count):
        fields = random_fields(rng, index)
        sample_id = f"bank_card_{index:03d}"
        base_image = render_card(fields, seed + index, target_size)
        normal_image_path = ""
        for quality_type in QUALITY_TYPES:
            image_path = save_variant(base_image, output_dir, index, quality_type)
            if quality_type == "normal":
                normal_image_path = relative(image_path)
            labels.append(
                {
                    "sample_id": sample_id,
                    "doc_type": "bank_card",
                    "side": "front",
                    "quality_type": quality_type,
                    "image_path": relative(image_path),
                    "is_real_document": False,
                    "synthetic": True,
                    "warning": WATERMARK,
                    "fields": fields,
                }
            )
        catalog_records.append({"fields": fields, "normal_image_path": normal_image_path})

    existing_labels = load_existing_labels(labels_path)
    new_paths = {item["image_path"] for item in labels}
    generated_root = relative(output_dir)
    preserved_labels = [
        item
        for item in existing_labels
        if item.get("image_path") not in new_paths
        and not str(item.get("image_path", "")).startswith(generated_root + "/")
    ]
    write_json(labels_path, preserved_labels + labels)
    write_json(output_dir / "data.json", build_catalog(catalog_records, output_dir))
    return labels


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate marked synthetic bank-card OCR test images.")
    parser.add_argument("--count", type=int, default=5, help="number of bank-card records to generate")
    parser.add_argument("--start-index", type=int, default=1, help="first numeric suffix, e.g. 1 -> synthetic_debit_001.png")
    parser.add_argument("--seed", type=int, default=20260615, help="random seed for reproducible output")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="bank-card output directory")
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_PATH, help="JSON label output path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    labels = generate(args.count, args.output_dir, args.labels, args.seed, args.start_index)
    output_dir = resolve_project_path(args.output_dir)
    labels_path = resolve_project_path(args.labels)
    print(f"Generated {len(labels)} synthetic bank-card images in {relative(output_dir)}")
    print(f"Wrote catalog to {relative(output_dir / 'data.json')}")
    print(f"Wrote labels to {relative(labels_path)}")


if __name__ == "__main__":
    main()
