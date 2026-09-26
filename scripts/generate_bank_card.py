"""Generate synthetic bank card images and labels.

Run:
    python scripts/generate_bank_card.py

The images are synthetic OCR test assets. They do not use real bank logos,
real card artwork, or real customer data.

每张卡的卡号、持卡人、有效期、背景配色与装饰元素都由 ``rng`` 派生，
所以同一批里的任意两张卡都能肉眼区分 —— 这是数据集的基本要求：
如果 100 张卡长得一样，它们只能测「流水线能不能跑通」，
测不出「模型对哪一张判错了」。
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

SURNAMES = [
    "ZHANG", "LI", "WANG", "ZHAO", "CHEN", "LIU", "SUN", "ZHOU",
    "WU", "XU", "MA", "ZHU", "HU", "GUO", "HE", "LIN",
]
GIVEN_NAMES = [
    "SAN", "MING", "WEI", "LEI", "JIE", "YANG", "QI", "YI",
    "HAO", "NING", "FENG", "JUN", "TAO", "PENG", "BIN", "KAI",
]

#: 背景渐变色对。每张卡随机选一对，让整批卡在缩略图里就能区分开。
BACKGROUND_PAIRS = [
    ("#29465f", "#516b83"),
    ("#1f3a5f", "#3d6b8f"),
    ("#2d4a3e", "#5a8f6b"),
    ("#4a2c4f", "#7d5585"),
    ("#5f3a29", "#8f6b51"),
    ("#1f4a4a", "#3d8080"),
    ("#3f2d5f", "#6b5a8f"),
    ("#5f2935", "#8f5163"),
    ("#2d3f4a", "#5a7285"),
    ("#4a3f1f", "#85763d"),
]

ACCENT_COLORS = [
    "#f2c94c", "#e8a33d", "#6fcf97", "#56ccf2",
    "#bb6bd9", "#f2994a", "#eb5757", "#9b9b9b",
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


def draw_background_decor(
    draw: ImageDraw.ImageDraw,
    size: tuple[int, int],
    accent: tuple[int, int, int],
    rng: random.Random,
) -> None:
    """每张卡不同的装饰：圆、斜线、光带。

    这是让同批卡片「肉眼可辨」的主要手段 —— 只改卡号的话，
    100 张卡在缩略图上仍然是一样的。
    """
    width, height = size

    # 两到三个随机位置、随机大小的半透明装饰圆
    for _ in range(rng.randint(2, 3)):
        cx = rng.randint(-120, width + 120)
        cy = rng.randint(-120, height + 120)
        radius = rng.randint(110, 260)
        color = accent if rng.random() < 0.6 else (255, 255, 255)
        alpha = rng.randint(28, 70)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*color, alpha))

    # 随机角度的斜线束
    angle_x = rng.randint(-80, 80)
    angle_y = rng.randint(-80, 80)
    for idx in range(rng.randint(2, 5)):
        offset = idx * rng.randint(40, 90)
        draw.line(
            (offset, angle_y + offset // 2, width + offset, angle_x + offset // 2),
            fill=(255, 255, 255, rng.randint(30, 80)),
            width=rng.randint(2, 6),
        )

    # 随机位置的一条宽光带
    if rng.random() < 0.7:
        band_y = rng.randint(0, height)
        draw.line(
            (0, band_y, width, band_y + rng.randint(-120, 120)),
            fill=(255, 255, 255, rng.randint(18, 42)),
            width=rng.randint(20, 60),
        )


def luhn_check_digit(prefix: str) -> str:
    """给前缀补上 Luhn 校验位，让卡号看起来是「真的」卡号结构。"""
    total = 0
    for idx, digit in enumerate(reversed(prefix)):
        value = int(digit)
        if idx % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


def make_card_number(rng: random.Random) -> str:
    """生成 16 位卡号并分组为 4-4-4-4。

    必须保持 16 位纯数字：``app.field_parser`` 要求 normalize 后是 16–19 位，
    而 ``app.rule_check.is_valid_card_number`` 用 ``fullmatch(r"\\d{16,19}")``。
    卡号一旦不合法，最终结论会变成 ``reject`` 而不是 ``review``，
    质量原因码就没机会被检验了。
    """
    prefix = str(rng.randint(4, 6))
    body = "".join(str(rng.randint(0, 9)) for _ in range(14))
    digits = prefix + body
    digits += luhn_check_digit(digits)
    return " ".join(digits[idx : idx + 4] for idx in range(0, 16, 4))


def make_name(rng: random.Random) -> str:
    return f"{rng.choice(SURNAMES)} {rng.choice(GIVEN_NAMES)}"


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


def draw_card(
    fields: dict[str, str],
    template: dict[str, object],
    path: Path,
    rng: random.Random,
) -> None:
    image = Image.new("RGB", CARD_SIZE, (20, 35, 52))
    layer = Image.new("RGBA", CARD_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # 背景色对从池里随机抽，而不是一律用模板那对
    default_pairs = BACKGROUND_PAIRS
    start, end = rng.choice(default_pairs)
    draw_gradient(draw, CARD_SIZE, start, end)
    draw.rounded_rectangle((0, 0, CARD_SIZE[0] - 1, CARD_SIZE[1] - 1), radius=34, outline=(230, 238, 245, 90), width=3)

    accent_hex = rng.choice(ACCENT_COLORS)
    accent = hex_to_rgb(accent_hex)
    draw_background_decor(draw, CARD_SIZE, accent, rng)

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
    template = load_template()
    issuer = str(template.get("issuer", "TEST BANK"))
    card_type = str(template.get("card_type", "Synthetic Debit Card"))

    bank_labels: list[dict[str, object]] = []
    for index in range(1, count + 1):
        # 每个 index 一个独立种子：增删样本不会让整批卡面错位
        rng = random.Random(f"{seed}-bank-{index:04d}")
        fields = {
            "name": make_name(rng),
            "card_number": make_card_number(rng),
            "valid_date": make_valid_date(rng),
            "issuer": issuer,
            "card_type": card_type,
        }
        image_path = OUTPUT_DIR / f"bank_card_{index:04d}.png"
        draw_card(fields, template, image_path, rng)
        bank_labels.append(
            {
                "sample_id": f"bank_card_{index:04d}",
                "image_path": relative(image_path),
                "doc_type": "bank_card",
                "quality_type": "normal",
                "is_synthetic": True,
                "source": "generated_synthetic_bank_card",
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
