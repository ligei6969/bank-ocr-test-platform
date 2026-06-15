"""Generate ID-card-like OCR test images with GPL-3.0 idcardgenerator assets.

This script uses assets from:
https://github.com/airob0t/idcardgenerator

Those assets are licensed under GPL-3.0. Keep this script and generated asset
usage in GPL-compatible workflows.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT_DIR = Path(__file__).resolve().parents[1]
GPL_ROOT = ROOT_DIR / "third_party" / "idcardgenerator"
RES_DIR = GPL_ROOT / "idcardgenerator" / "usedres"
TEMPLATE_PATH = RES_DIR / "empty.png"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "synthetic" / "id_card"
DEFAULT_LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"

QUALITY_TYPES = ("normal", "blur", "glare", "occlusion", "rotate")
FULL_CROP = (0, 0, 2480, 3508)
FRONT_CROP = (290, 500, 2195, 1696)
BACK_CROP = (285, 1932, 2200, 3135)
WATERMARK = "\u4ec5\u4f9bOCR\u6d4b\u8bd5 \u975e\u771f\u5b9e\u8bc1\u4ef6"

SURNAMES = "\u8d75\u94b1\u5b59\u674e\u5468\u5434\u90d1\u738b\u51af\u9648\u891a\u536b\u848b\u6c88\u97e9\u6768\u6731\u79e6\u5c24\u8bb8\u4f55\u5415\u65bd\u5f20\u5b54\u66f9\u4e25\u534e\u91d1\u9b4f\u9676\u59dc"
GIVEN_CHARS = "\u6668\u5b87\u6893\u6db5\u4e00\u8bfa\u5b50\u8f69\u96e8\u6850\u6b23\u6021\u6d69\u7136\u5609\u601d\u6e90\u660e\u82e5\u66e6\u8bd7\u4fca\u6770\u4f73\u5b81"
SEXES = ["\u7537", "\u5973"]
NATIONS = ["\u6c49", "\u6ee1", "\u56de", "\u82d7", "\u58ee", "\u8499\u53e4", "\u571f\u5bb6"]
PROVINCES = ["\u6c5f\u5357\u7701", "\u6d77\u4e1c\u7701", "\u5b89\u5317\u7701", "\u4e91\u5ddd\u7701", "\u6cb3\u897f\u7701"]
CITIES = ["\u9752\u5ddd\u5e02", "\u6d77\u6797\u5e02", "\u4e39\u6c5f\u5e02", "\u660e\u6e2f\u5e02", "\u4e1c\u5b81\u5e02", "\u6c38\u5b89\u5e02"]
DISTRICTS = ["\u57ce\u4e1c\u533a", "\u57ce\u897f\u533a", "\u6d77\u6e7e\u533a", "\u6cb3\u6ee8\u533a", "\u65b0\u57ce\u533a", "\u4e1c\u6e56\u533a"]
STREETS = ["\u6c5f\u5357\u8def", "\u65b0\u6c11\u8857", "\u6d77\u68e0\u5df7", "\u5efa\u8bbe\u5927\u9053", "\u957f\u5b81\u8def", "\u6587\u660c\u8857"]
ISSUER_SUFFIXES = ["\u516c\u5b89\u5c40", "\u516c\u5b89\u5c40\u57ce\u4e1c\u5206\u5c40", "\u516c\u5b89\u5c40\u65b0\u57ce\u5206\u5c40"]


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(RES_DIR / name), size=size)


FONTS = {
    "name": font("hei.ttf", 72),
    "other": font("hei.ttf", 60),
    "birth": font("fzhei.ttf", 60),
    "id": font("ocrb10bt.ttf", 72),
    "watermark": font("hei.ttf", 82),
}


def resolve_project_path(path: Path) -> Path:
    return path if path.is_absolute() else ROOT_DIR / path


def relative(path: Path) -> str:
    return path.relative_to(ROOT_DIR).as_posix()


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
    province = rng.choice(PROVINCES)
    city = rng.choice(CITIES)
    address = f"{province}{city}{rng.choice(DISTRICTS)}{rng.choice(STREETS)}{rng.randint(1, 299)}\u53f7"
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
        "id_number": f"000000{birth:%Y%m%d}{index:03d}{rng.randint(0, 99):02d}X",
        "issuer": f"{city}{rng.choice(ISSUER_SUFFIXES)}",
        "valid_period": f"{issue_start:%Y.%m.%d}-{issue_end:%Y.%m.%d}",
    }


def draw_avatar(image: Image.Image, rng: random.Random) -> None:
    avatar = Image.new("RGBA", (500, 670), (232, 214, 176, 255))
    draw = ImageDraw.Draw(avatar)
    body = rng.choice([(249, 183, 53), (63, 80, 106), (79, 164, 128), (180, 92, 83)])
    face = rng.choice([(238, 198, 172), (224, 177, 142), (242, 206, 182)])
    draw.ellipse((120, 75, 380, 420), fill=face)
    draw.arc((102, 30, 398, 300), 182, 358, fill=(35, 43, 43), width=70)
    draw.ellipse((195, 230, 218, 254), fill=(35, 35, 35))
    draw.ellipse((282, 230, 305, 254), fill=(35, 35, 35))
    draw.line((235, 320, 270, 320), fill=(130, 68, 72), width=8)
    draw.polygon([(65, 670), (435, 670), (350, 425), (150, 425)], fill=body)
    draw.polygon([(175, 425), (325, 425), (275, 560), (225, 560)], fill=(248, 248, 240))
    image.alpha_composite(avatar, dest=(1500, 690))


def draw_watermark(image: Image.Image) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.text((740, 1550), WATERMARK, font=FONTS["watermark"], fill=(214, 36, 42, 72))
    draw.text((740, 2940), WATERMARK, font=FONTS["watermark"], fill=(214, 36, 42, 72))
    rotated = layer.rotate(-14, resample=Image.Resampling.BICUBIC, center=(1240, 1754))
    image.alpha_composite(rotated)


def draw_fields(fields: dict[str, str], seed: int) -> Image.Image:
    rng = random.Random(seed)
    image = Image.open(TEMPLATE_PATH).convert("RGBA")
    draw = ImageDraw.Draw(image)

    draw.text((630, 690), fields["name"], fill=(0, 0, 0), font=FONTS["name"])
    draw.text((630, 840), fields["sex"], fill=(0, 0, 0), font=FONTS["other"])
    draw.text((1030, 840), fields["nation"], fill=(0, 0, 0), font=FONTS["other"])
    draw.text((630, 980), fields["birth_year"], fill=(0, 0, 0), font=FONTS["birth"])
    draw.text((950, 980), fields["birth_month"], fill=(0, 0, 0), font=FONTS["birth"])
    draw.text((1150, 980), fields["birth_day"], fill=(0, 0, 0), font=FONTS["birth"])

    start = 0
    y = 1120
    while start + 11 < len(fields["address"]):
        draw.text((630, y), fields["address"][start : start + 11], fill=(0, 0, 0), font=FONTS["other"])
        start += 11
        y += 100
    draw.text((630, y), fields["address"][start:], fill=(0, 0, 0), font=FONTS["other"])

    draw.text((950, 1475), fields["id_number"], fill=(0, 0, 0), font=FONTS["id"])
    draw.text((1050, 2750), fields["issuer"], fill=(0, 0, 0), font=FONTS["other"])
    draw.text((1050, 2895), fields["valid_period"], fill=(0, 0, 0), font=FONTS["other"])
    draw_avatar(image, rng)
    draw_watermark(image)
    return image.convert("RGB")


def add_glare(image: Image.Image) -> Image.Image:
    result = image.convert("RGBA")
    overlay = Image.new("RGBA", result.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = result.size
    draw.ellipse((w * 0.52, h * 0.04, w * 1.08, h * 0.70), fill=(255, 255, 255, 98))
    draw.polygon([(w * 0.20, 0), (w * 0.34, 0), (w, h), (w * 0.82, h)], fill=(255, 255, 255, 50))
    result.alpha_composite(overlay)
    return result.convert("RGB")


def add_occlusion(image: Image.Image) -> Image.Image:
    result = image.copy()
    draw = ImageDraw.Draw(result)
    w, h = result.size
    draw.rectangle((int(w * 0.58), int(h * 0.18), int(w * 0.88), int(h * 0.28)), fill=(28, 32, 38))
    return result


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
        return image.rotate(4, resample=Image.Resampling.BICUBIC, fillcolor=(0, 0, 0))
    raise ValueError(f"Unsupported quality type: {quality_type}")


def save_image(image: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def output_name(prefix: str, index: int) -> str:
    return f"{prefix}{index:03d}.png"


def generate(count: int, output_dir: Path, labels_path: Path, seed: int, start_index: int) -> list[dict[str, object]]:
    output_dir = resolve_project_path(output_dir)
    labels_path = resolve_project_path(labels_path)
    rng = random.Random(seed)
    labels: list[dict[str, object]] = []

    for index in range(start_index, start_index + count):
        fields = random_fields(rng, index)
        sample_id = f"id_card_{index:03d}"
        full = draw_fields(fields, seed + index)
        front = full.crop(FRONT_CROP)
        back = full.crop(BACK_CROP)

        full_path = save_image(full, output_dir / "full" / "normal" / output_name("idcard", index))
        labels.append(
            {
                "sample_id": sample_id,
                "doc_type": "id_card",
                "side": "full",
                "quality_type": "normal",
                "image_path": relative(full_path),
                "source": "GPL-3.0 airob0t/idcardgenerator assets",
                "is_real_document": False,
                "warning": WATERMARK,
                "fields": fields,
            }
        )

        for side, base_image in (("front", front), ("back", back)):
            for quality_type in QUALITY_TYPES:
                path = save_image(
                    apply_quality(base_image, quality_type),
                    output_dir / side / quality_type / output_name(side, index),
                )
                labels.append(
                    {
                        "sample_id": sample_id,
                        "doc_type": "id_card",
                        "side": side,
                        "quality_type": quality_type,
                        "image_path": relative(path),
                        "source": "GPL-3.0 airob0t/idcardgenerator assets",
                        "is_real_document": False,
                        "warning": WATERMARK,
                        "fields": fields,
                    }
                )

    existing_labels = []
    if labels_path.exists():
        existing_labels = json.loads(labels_path.read_text(encoding="utf-8"))
    preserved_labels = [item for item in existing_labels if item.get("doc_type") != "id_card"]
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels_path.write_text(json.dumps(preserved_labels + labels, ensure_ascii=False, indent=2), encoding="utf-8")
    return labels


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GPL-template ID-card OCR test images.")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260615)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    labels = generate(args.count, args.output_dir, args.labels, args.seed, args.start_index)
    print(f"Generated {len(labels)} GPL-template ID-card images")
    print(f"Wrote labels to {relative(resolve_project_path(args.labels))}")


if __name__ == "__main__":
    main()
