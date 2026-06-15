"""Generate marked synthetic ID-card images using local samples as style guides.

The output follows:

data/synthetic/id_card/{front,back}/{normal,blur,glare,occlusion,rotate}/

The sample paths used as layout references are:
- data/synthetic/id_card/front/normal/front001.png
- data/synthetic/id_card/back/normal/back001.png

Generated images are marked as OCR test samples and use invalid ID numbers.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "synthetic" / "id_card"
DEFAULT_LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"
FRONT_SAMPLE_PATH = DEFAULT_OUTPUT_DIR / "front" / "normal" / "front001.png"
BACK_SAMPLE_PATH = DEFAULT_OUTPUT_DIR / "back" / "normal" / "back001.png"

FRONT_SIZE = (583, 386)
BACK_SIZE = (564, 370)
RENDER_SCALE = 4
QUALITY_TYPES = ("normal", "blur", "glare", "occlusion", "rotate")

BLUE = (44, 126, 185)
BLACK = (28, 31, 34)
RED = (210, 42, 44)
WATERMARK = "\u4ec5\u4f9bOCR\u6d4b\u8bd5 \u975e\u771f\u5b9e\u8bc1\u4ef6"

SURNAMES = "\u8d75\u94b1\u5b59\u674e\u5468\u5434\u90d1\u738b\u51af\u9648\u891a\u536b\u848b\u6c88\u97e9\u6768\u6731\u79e6\u5c24\u8bb8\u4f55\u5415\u65bd\u5f20\u5b54\u66f9\u4e25\u534e\u91d1\u9b4f\u9676\u59dc"
GIVEN_CHARS = "\u6668\u5b87\u6893\u6db5\u4e00\u8bfa\u5b50\u8f69\u96e8\u6850\u6b23\u6021\u6d69\u7136\u5609\u601d\u6e90\u660e\u82e5\u66e6\u8bd7\u4fca\u6770\u4f73\u5b81"
SEXES = ["\u7537", "\u5973"]
NATIONS = ["\u6c49", "\u6ee1", "\u56de", "\u82d7", "\u58ee", "\u8499\u53e4", "\u571f\u5bb6"]
PROVINCES = [
    "\u6c5f\u5357\u7701",
    "\u6d77\u4e1c\u7701",
    "\u5b89\u5317\u7701",
    "\u4e91\u5ddd\u7701",
    "\u6cb3\u897f\u7701",
]
CITIES = [
    "\u9752\u5ddd\u5e02",
    "\u6d77\u6797\u5e02",
    "\u4e39\u6c5f\u5e02",
    "\u660e\u6e2f\u5e02",
    "\u4e1c\u5b81\u5e02",
    "\u6c38\u5b89\u5e02",
]
STREETS = [
    "\u6c5f\u5357\u8def",
    "\u65b0\u6c11\u8857",
    "\u6d77\u68e0\u5df7",
    "\u5efa\u8bbe\u5927\u9053",
    "\u957f\u5b81\u8def",
    "\u6587\u660c\u8857",
    "\u4e2d\u5c71\u8def",
    "\u548c\u5e73\u8857",
]
DISTRICTS = ["\u57ce\u4e1c\u533a", "\u57ce\u897f\u533a", "\u6d77\u6e7e\u533a", "\u6cb3\u6ee8\u533a", "\u65b0\u57ce\u533a", "\u4e1c\u6e56\u533a"]
ISSUER_SUFFIXES = [
    "\u516c\u5b89\u5c40",
    "\u516c\u5b89\u5c40\u57ce\u4e1c\u5206\u5c40",
    "\u516c\u5b89\u5c40\u65b0\u57ce\u5206\u5c40",
    "\u516c\u5b89\u5c40\u6237\u653f\u7ba1\u7406\u5927\u961f",
]


def resolve_project_path(path: Path) -> Path:
    return path if path.is_absolute() else ROOT_DIR / path


def relative(path: Path) -> str:
    return path.relative_to(ROOT_DIR).as_posix()


def resolve_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def build_fonts(scale: int) -> dict[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    return {
        "label": resolve_font(17 * scale, bold=True),
        "field": resolve_font(20 * scale),
        "field_bold": resolve_font(23 * scale, bold=True),
        "id": resolve_font(23 * scale, bold=True),
        "title": resolve_font(31 * scale, bold=True),
        "back_title": resolve_font(43 * scale, bold=True),
        "small": resolve_font(18 * scale, bold=True),
        "watermark": resolve_font(24 * scale, bold=True),
        "seal": resolve_font(16 * scale, bold=True),
        "photo": resolve_font(11 * scale, bold=True),
    }


FONTS = build_fonts(1)


def sample_size(path: Path, fallback: tuple[int, int]) -> tuple[int, int]:
    if path.exists():
        with Image.open(path) as image:
            return image.size
    return fallback


def scaled_size(size: tuple[int, int], scale: int) -> tuple[int, int]:
    return (size[0] * scale, size[1] * scale)


def downsample(image: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    return image.resize(target_size, resample=Image.Resampling.LANCZOS)


def wood_background(size: tuple[int, int], seed: int) -> Image.Image:
    rng = random.Random(seed)
    width, height = size
    image = Image.new("RGB", size, (219, 185, 145))
    pixels = image.load()
    for y in range(height):
        base = 205 + int(14 * math.sin(y * 0.095)) + rng.randint(-2, 2)
        for x in range(width):
            grain = int(10 * math.sin((x + y * 1.8) * 0.035)) + rng.randint(-5, 5)
            r = max(0, min(255, base + grain + 16))
            g = max(0, min(255, base + grain - 14))
            b = max(0, min(255, base + grain - 50))
            pixels[x, y] = (r, g, b)
    return image.convert("RGBA")


def rounded_card_layer(size: tuple[int, int], margin: int, radius: int, seed: int, scale: int = 1) -> Image.Image:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rect = (margin, margin + 5, size[0] - margin - 2, size[1] - margin - 8)
    draw.rounded_rectangle(
        rect,
        radius=radius,
        fill=(225, 247, 248, 244),
        outline=(121, 169, 178, 190),
        width=max(1, scale),
    )
    draw_security_pattern(draw, rect, seed, scale=scale)
    return image


def draw_security_pattern(draw: ImageDraw.ImageDraw, rect: tuple[int, int, int, int], seed: int, scale: int = 1) -> None:
    rng = random.Random(seed)
    x0, y0, x1, y1 = rect
    width = x1 - x0
    height = y1 - y0
    for idx in range(34):
        color = rng.choice([(78, 176, 211, 90), (221, 92, 132, 65), (67, 150, 194, 65)])
        phase = rng.random() * math.tau
        amp = rng.randint(7 * scale, 18 * scale)
        freq = rng.uniform(0.024, 0.046)
        y_base = y0 + 14 * scale + idx * max(7 * scale, height // 33)
        points = []
        for x in range(x0 + 6 * scale, x1 - 6 * scale, 7 * scale):
            y = y_base + math.sin(x * freq / scale + phase) * amp + math.sin(x * freq * 1.6 / scale) * 4 * scale
            points.append((x, y))
        draw.line(points, fill=color, width=max(1, scale // 2))

    for idx in range(24):
        color = rng.choice([(68, 165, 205, 65), (215, 101, 140, 48)])
        x_base = x0 + 14 * scale + idx * max(12 * scale, width // 24)
        points = []
        for y in range(y0 + 6 * scale, y1 - 6 * scale, 7 * scale):
            x = x_base + math.sin(y * 0.036 / scale + idx) * 12 * scale
            points.append((x, y))
        draw.line(points, fill=color, width=max(1, scale // 2))


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font_key: str,
    fill: tuple[int, int, int] = BLACK,
) -> None:
    draw.text(xy, text, font=FONTS[font_key], fill=fill)


def spoint(x: int, y: int, scale: int) -> tuple[int, int]:
    return (x * scale, y * scale)


def sbox(box: tuple[int, int, int, int], scale: int) -> tuple[int, int, int, int]:
    return tuple(value * scale for value in box)  # type: ignore[return-value]


def scaled_polygon(points: list[tuple[int, int]], scale: int) -> list[tuple[int, int]]:
    return [(x * scale, y * scale) for x, y in points]


def draw_watermark(image: Image.Image, center: tuple[int, int], scale: int = 1) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.text(
        (center[0] - 150 * scale, center[1] - 18 * scale),
        WATERMARK,
        font=FONTS["watermark"],
        fill=(210, 40, 40, 86),
    )
    rotated = layer.rotate(-15, resample=Image.Resampling.BICUBIC, center=center)
    image.alpha_composite(rotated)


def random_birth(rng: random.Random) -> date:
    start = date(1970, 1, 1)
    end = date(2005, 12, 31)
    return start + timedelta(days=rng.randint(0, (end - start).days))


def random_name(rng: random.Random) -> str:
    return rng.choice(SURNAMES) + "".join(rng.choice(GIVEN_CHARS) for _ in range(rng.choice([1, 2])))


def random_fields(rng: random.Random, index: int) -> dict[str, str]:
    birth = random_birth(rng)
    issue_start = date(birth.year + 18, rng.randint(1, 12), rng.randint(1, 28))
    issue_end = date(issue_start.year + 10, issue_start.month, issue_start.day)
    serial = f"{index:03d}{rng.randint(0, 99):02d}"
    province = rng.choice(PROVINCES)
    city = rng.choice(CITIES)
    district = rng.choice(DISTRICTS)
    street = rng.choice(STREETS)
    address = f"{province}{city}{district}{street}{rng.randint(1, 299)}\u53f7"
    if rng.random() < 0.5:
        address += f"{rng.randint(1, 18)}\u680b{rng.randint(101, 2806)}\u5ba4"
    return {
        "name": random_name(rng),
        "sex": rng.choice(SEXES),
        "nation": rng.choice(NATIONS),
        "birth_year": str(birth.year),
        "birth_month": str(birth.month),
        "birth_day": str(birth.day),
        "address": address,
        "id_number": f"000000{birth:%Y%m%d}{serial}X",
        "issuer": f"{city}{rng.choice(ISSUER_SUFFIXES)}",
        "valid_period": f"{issue_start:%Y.%m.%d}-{issue_end:%Y.%m.%d}",
    }


def draw_portrait(draw: ImageDraw.ImageDraw, scale: int = 1) -> None:
    draw.rectangle(sbox((363, 89, 528, 280), scale), fill=(216, 228, 225))
    draw.ellipse(sbox((415, 123, 482, 202), scale), fill=(235, 205, 184), outline=(135, 115, 105), width=scale)
    draw.arc(sbox((399, 104, 496, 184), scale), 185, 355, fill=(42, 51, 51), width=18 * scale)
    draw.ellipse(sbox((435, 160, 441, 166), scale), fill=(45, 45, 45))
    draw.ellipse(sbox((461, 160, 467, 166), scale), fill=(45, 45, 45))
    draw.line((*spoint(445, 180, scale), *spoint(458, 180, scale)), fill=(138, 70, 75), width=2 * scale)
    draw.polygon(scaled_polygon([(394, 280), (510, 280), (485, 216), (421, 216)], scale), fill=(35, 43, 49))
    draw.polygon(scaled_polygon([(429, 216), (476, 216), (462, 247), (443, 247)], scale), fill=(239, 241, 237))
    draw.text(spoint(404, 252, scale), "PHOTO SAMPLE", font=FONTS["photo"], fill=(125, 132, 136))


def render_front(fields: dict[str, str], seed: int) -> Image.Image:
    global FONTS
    target_size = sample_size(FRONT_SAMPLE_PATH, FRONT_SIZE)
    scale = RENDER_SCALE
    size = scaled_size(target_size, scale)
    FONTS = build_fonts(scale)
    image = wood_background(size, seed)
    image.alpha_composite(rounded_card_layer(size, 8 * scale, 18 * scale, seed + 11, scale=scale))
    draw = ImageDraw.Draw(image)

    draw_text(draw, spoint(55, 75, scale), "\u59d3  \u540d", "label", BLUE)
    draw_text(draw, spoint(122, 68, scale), fields["name"], "field_bold")
    draw_text(draw, spoint(55, 120, scale), "\u6027  \u522b", "label", BLUE)
    draw_text(draw, spoint(122, 115, scale), fields["sex"], "field")
    draw_text(draw, spoint(218, 120, scale), "\u6c11  \u65cf", "label", BLUE)
    draw_text(draw, spoint(294, 115, scale), fields["nation"], "field")
    draw_text(draw, spoint(55, 166, scale), "\u51fa  \u751f", "label", BLUE)
    draw_text(draw, spoint(122, 160, scale), fields["birth_year"], "field")
    draw_text(draw, spoint(214, 166, scale), "\u5e74", "label", BLUE)
    draw_text(draw, spoint(264, 160, scale), fields["birth_month"], "field")
    draw_text(draw, spoint(315, 166, scale), "\u6708", "label", BLUE)
    draw_text(draw, spoint(365, 160, scale), fields["birth_day"], "field")
    draw_text(draw, spoint(414, 166, scale), "\u65e5", "label", BLUE)
    draw_text(draw, spoint(55, 211, scale), "\u4f4f  \u5740", "label", BLUE)
    draw_text(draw, spoint(122, 205, scale), fields["address"][:13], "field")
    draw_text(draw, spoint(122, 237, scale), fields["address"][13:], "field")
    draw_text(draw, spoint(55, 315, scale), "\u516c\u6c11\u8eab\u4efd\u53f7\u7801", "label", BLUE)
    draw_text(draw, spoint(207, 308, scale), fields["id_number"], "id")
    draw_portrait(draw, scale=scale)
    draw_watermark(image, spoint(300, 214, scale), scale=scale)
    return downsample(image.convert("RGB"), target_size)


def draw_test_seal(draw: ImageDraw.ImageDraw, scale: int = 1) -> None:
    cx, cy, radius = 76 * scale, 84 * scale, 53 * scale
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=RED, width=4 * scale)
    draw.ellipse((cx - radius + 9 * scale, cy - radius + 9 * scale, cx + radius - 9 * scale, cy + radius - 9 * scale), outline=RED, width=scale)
    draw.regular_polygon((cx, cy - 4 * scale, 24 * scale), n_sides=5, rotation=-90, fill=RED)
    draw.text(spoint(52, 113, scale), "TEST", font=FONTS["seal"], fill=RED)


def render_back(fields: dict[str, str], seed: int) -> Image.Image:
    global FONTS
    target_size = sample_size(BACK_SAMPLE_PATH, BACK_SIZE)
    scale = RENDER_SCALE
    size = scaled_size(target_size, scale)
    FONTS = build_fonts(scale)
    image = wood_background(size, seed + 1000)
    image.alpha_composite(rounded_card_layer(size, 4 * scale, 18 * scale, seed + 10011, scale=scale))
    draw = ImageDraw.Draw(image)
    draw_test_seal(draw, scale=scale)

    draw_text(draw, spoint(178, 48, scale), "OCR\u6d4b\u8bd5\u6837\u5361", "title", BLACK)
    draw_text(draw, spoint(171, 105, scale), "\u975e\u771f\u5b9e\u5c45\u6c11\u8eab\u4efd\u8bc1", "back_title", BLACK)

    draw_text(draw, spoint(140, 270, scale), "\u7b7e\u53d1\u673a\u5173", "small", BLACK)
    draw_text(draw, spoint(231, 267, scale), fields["issuer"], "field")
    draw_text(draw, spoint(140, 315, scale), "\u6709\u6548\u671f\u9650", "small", BLACK)
    draw_text(draw, spoint(231, 312, scale), fields["valid_period"], "field")
    draw_watermark(image, spoint(284, 187, scale), scale=scale)
    return downsample(image.convert("RGB"), target_size)


def add_glare(image: Image.Image) -> Image.Image:
    result = image.convert("RGBA")
    overlay = Image.new("RGBA", result.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = result.size
    draw.ellipse((w * 0.52, h * 0.03, w * 1.08, h * 0.75), fill=(255, 255, 255, 112))
    draw.polygon([(w * 0.18, 0), (w * 0.31, 0), (w * 0.98, h), (w * 0.80, h)], fill=(255, 255, 255, 58))
    result.alpha_composite(overlay)
    return result.convert("RGB")


def add_occlusion(image: Image.Image) -> Image.Image:
    result = image.copy()
    draw = ImageDraw.Draw(result)
    w, _ = result.size
    draw.rounded_rectangle((w - 236, 58, w - 27, 112), radius=6, fill=(42, 48, 54))
    draw.text((w - 214, 75), "OCR TEST MASK", font=resolve_font(22, bold=True), fill=(245, 245, 245))
    return result


def rotate_image(image: Image.Image) -> Image.Image:
    return image.rotate(5, resample=Image.Resampling.BICUBIC, fillcolor=(222, 188, 148))


def apply_quality(image: Image.Image, quality_type: str) -> Image.Image:
    if quality_type == "normal":
        return image
    if quality_type == "blur":
        return image.filter(ImageFilter.GaussianBlur(radius=1.8))
    if quality_type == "glare":
        return add_glare(image)
    if quality_type == "occlusion":
        return add_occlusion(image)
    if quality_type == "rotate":
        return rotate_image(image)
    raise ValueError(f"Unsupported quality type: {quality_type}")


def image_filename(side: str, index: int) -> str:
    return f"{side}{index:03d}.png"


def save_variant(base_image: Image.Image, output_dir: Path, index: int, side: str, quality_type: str) -> Path:
    path = output_dir / side / quality_type / image_filename(side, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    apply_quality(base_image, quality_type).save(path)
    return path


def generate(count: int, output_dir: Path, labels_path: Path, seed: int, start_index: int) -> list[dict[str, object]]:
    output_dir = resolve_project_path(output_dir)
    labels_path = resolve_project_path(labels_path)
    rng = random.Random(seed)
    labels: list[dict[str, object]] = []

    for index in range(start_index, start_index + count):
        fields = random_fields(rng, index)
        sample_id = f"id_card_{index:03d}"
        side_images = {
            "front": render_front(fields, seed + index),
            "back": render_back(fields, seed + index),
        }
        for side, base_image in side_images.items():
            for quality_type in QUALITY_TYPES:
                image_path = save_variant(base_image, output_dir, index, side, quality_type)
                labels.append(
                    {
                        "sample_id": sample_id,
                        "doc_type": "id_card",
                        "side": side,
                        "quality_type": quality_type,
                        "image_path": relative(image_path),
                        "is_real_document": False,
                        "warning": WATERMARK,
                        "fields": fields,
                    }
                )

    labels_path.parent.mkdir(parents=True, exist_ok=True)
    existing_labels = []
    if labels_path.exists():
        existing_labels = json.loads(labels_path.read_text(encoding="utf-8"))
    new_paths = {item["image_path"] for item in labels}
    preserved_labels = [
        item
        for item in existing_labels
        if item.get("doc_type") != "id_card" and item.get("image_path") not in new_paths
    ]
    merged_labels = preserved_labels + labels
    labels_path.write_text(json.dumps(merged_labels, ensure_ascii=False, indent=2), encoding="utf-8")
    return labels


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate marked synthetic ID-card OCR test images.")
    parser.add_argument("--count", type=int, default=5, help="number of ID-card records to generate")
    parser.add_argument("--start-index", type=int, default=1, help="first numeric suffix, e.g. 1 -> front001.png")
    parser.add_argument("--seed", type=int, default=20260615, help="random seed for reproducible output")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="base id_card output directory")
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_PATH, help="JSON label output path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    labels = generate(args.count, args.output_dir, args.labels, args.seed, args.start_index)
    output_dir = resolve_project_path(args.output_dir)
    labels_path = resolve_project_path(args.labels)
    print(f"Generated {len(labels)} synthetic ID-card images in {relative(output_dir)}")
    print(f"Wrote labels to {relative(labels_path)}")


if __name__ == "__main__":
    main()
