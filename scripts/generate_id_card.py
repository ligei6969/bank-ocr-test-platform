"""Generate synthetic ID-card front/back OCR test images with GPL assets.

Run:
    python scripts/generate_id_card.py --count 100

This uses vendored GPL-3.0 assets from airob0t/idcardgenerator:
    third_party/idcardgenerator/idcardgenerator/usedres/

Generated data is for OCR testing only. ID numbers use an invalid administrative
code and every image is marked with a non-real-document watermark.
"""

from __future__ import annotations

import argparse
import json
import random
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT_DIR / "data" / "processed" / "id_card"
LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"
AVATAR_POOL_DIR = ROOT_DIR / "data" / "templates" / "id_card" / "avatar_pool"
NAILONG_DIR = ROOT_DIR / "nailong_img"
GPL_RES_DIR = ROOT_DIR / "third_party" / "idcardgenerator" / "idcardgenerator" / "usedres"
GPL_TEMPLATE_PATH = GPL_RES_DIR / "empty.png"

FRONT_CROP = (290, 500, 2195, 1696)
BACK_CROP = (285, 1932, 2200, 3135)
AVATAR_BOX = (1500, 690, 2000, 1360)
WATERMARK = "\u4ec5\u4f9bOCR\u6d4b\u8bd5 \u975e\u771f\u5b9e\u8bc1\u4ef6"
QUALITY_JPEG = 95
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

SURNAMES = "\u8d75\u94b1\u5b59\u674e\u5468\u5434\u90d1\u738b\u51af\u9648\u891a\u536b\u848b\u6c88\u97e9\u6768\u6731\u79e6\u5c24\u8bb8\u4f55\u5415\u65bd\u5f20\u5b54\u66f9\u4e25\u534e\u91d1\u9b4f\u9676\u59dc"
GIVEN_CHARS = "\u6668\u5b87\u6893\u6db5\u4e00\u8bfa\u5b50\u8f69\u96e8\u6850\u6b23\u6021\u6d69\u7136\u5609\u601d\u6e90\u660e\u82e5\u66e6\u8bd7\u4fca\u6770\u4f73\u5b81"
GENDERS = ["\u7537", "\u5973"]
NATIONS = ["\u6c49", "\u6ee1", "\u56de", "\u82d7", "\u58ee", "\u8499\u53e4", "\u571f\u5bb6"]
PROVINCES = ["\u5b89\u5317\u7701", "\u6d77\u4e1c\u7701", "\u4e1c\u6e56\u7701", "\u4e91\u5ddd\u7701", "\u6cb3\u897f\u7701"]
CITIES = ["\u660e\u6e2f\u5e02", "\u9752\u5ddd\u5e02", "\u6d77\u6797\u5e02", "\u4e39\u6c5f\u5e02", "\u4e1c\u5b81\u5e02", "\u6c38\u5b89\u5e02"]
DISTRICTS = ["\u57ce\u897f\u533a", "\u57ce\u4e1c\u533a", "\u6d77\u6e7e\u533a", "\u6cb3\u6ee8\u533a", "\u65b0\u57ce\u533a", "\u4e1c\u6e56\u533a"]
STREETS = ["\u6d77\u68e0\u5df7", "\u6d4b\u8bd5\u8def", "\u6837\u672c\u8857", "\u5efa\u8bbe\u5927\u9053", "\u957f\u5b81\u8def", "\u6587\u660c\u8857"]
AUTHORITIES = [
    "\u660e\u6e2f\u5e02\u516c\u5b89\u5c40\u65b0\u57ce\u5206\u5c40",
    "\u6d4b\u8bd5\u516c\u5b89\u5c40",
    "\u6d4b\u8bd5\u516c\u5b89\u5c40\u57ce\u4e1c\u5206\u5c40",
    "\u6d4b\u8bd5\u516c\u5b89\u5c40\u6237\u653f\u7ba1\u7406\u5927\u961f",
]


def relative(path: Path) -> str:
    return path.relative_to(ROOT_DIR).as_posix()


def load_labels() -> list[dict[str, object]]:
    if not LABELS_PATH.exists():
        return []
    return json.loads(LABELS_PATH.read_text(encoding="utf-8"))


def write_labels(labels: list[dict[str, object]]) -> None:
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LABELS_PATH.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")


def gpl_font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = GPL_RES_DIR / name
    if path.exists():
        return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


FONTS = {
    "name": gpl_font("hei.ttf", 72),
    "field": gpl_font("hei.ttf", 60),
    "birth": gpl_font("fzhei.ttf", 60),
    "id": gpl_font("ocrb10bt.ttf", 72),
    "watermark": gpl_font("hei.ttf", 82),
}


def random_birth(rng: random.Random) -> date:
    start = date(1970, 1, 1)
    end = date(2005, 12, 31)
    return start + timedelta(days=rng.randint(0, (end - start).days))


def random_name(rng: random.Random) -> str:
    return rng.choice(SURNAMES) + "".join(rng.choice(GIVEN_CHARS) for _ in range(rng.choice([1, 2])))


def random_fields(rng: random.Random, index: int) -> tuple[dict[str, str], dict[str, str]]:
    birth = random_birth(rng)
    issue_start = date(birth.year + 18, rng.randint(1, 12), rng.randint(1, 28))
    issue_end = date(issue_start.year + 10, issue_start.month, issue_start.day)
    address = (
        f"{rng.choice(PROVINCES)}{rng.choice(CITIES)}{rng.choice(DISTRICTS)}"
        f"{rng.choice(STREETS)}{rng.randint(1, 299)}\u53f7"
    )
    if rng.random() < 0.45:
        address += f"{rng.randint(1, 18)}\u680b{rng.randint(101, 2806)}\u5ba4"

    front_fields = {
        "name": random_name(rng),
        "gender": rng.choice(GENDERS),
        "nation": rng.choice(NATIONS),
        "birth": birth.isoformat(),
        "address": address,
        "id_number": f"000000{birth:%Y%m%d}{index % 1000:03d}X",
    }
    back_fields = {
        "issue_authority": rng.choice(AUTHORITIES),
        "valid_period": f"{issue_start:%Y.%m.%d}-{issue_end:%Y.%m.%d}",
    }
    return front_fields, back_fields


def avatar_sources() -> list[Path]:
    AVATAR_POOL_DIR.mkdir(parents=True, exist_ok=True)
    primary = sorted(p for p in AVATAR_POOL_DIR.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    if primary:
        return primary
    if NAILONG_DIR.exists():
        return sorted(p for p in NAILONG_DIR.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    return []


def crop_to_cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    source_w, source_h = image.size
    target_w, target_h = size
    scale = max(target_w / source_w, target_h / source_h)
    resized = image.resize((int(source_w * scale), int(source_h * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def placeholder_avatar(size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGB", size, (232, 214, 176))
    draw = ImageDraw.Draw(image)
    w, h = size
    draw.ellipse((w * 0.30, h * 0.15, w * 0.72, h * 0.63), fill=(238, 198, 172))
    draw.pieslice((w * 0.20, h * 0.05, w * 0.82, h * 0.45), 180, 360, fill=(31, 42, 42))
    draw.ellipse((w * 0.42, h * 0.34, w * 0.47, h * 0.38), fill=(30, 30, 30))
    draw.ellipse((w * 0.58, h * 0.34, w * 0.63, h * 0.38), fill=(30, 30, 30))
    draw.line((w * 0.50, h * 0.46, w * 0.57, h * 0.46), fill=(125, 62, 70), width=8)
    draw.polygon([(w * 0.15, h), (w * 0.88, h), (w * 0.70, h * 0.63), (w * 0.32, h * 0.63)], fill=(183, 89, 80))
    draw.polygon([(w * 0.38, h * 0.63), (w * 0.64, h * 0.63), (w * 0.57, h * 0.83), (w * 0.45, h * 0.83)], fill=(246, 247, 240))
    return image


def load_avatar(rng: random.Random, sources: list[Path]) -> tuple[Image.Image, str | None]:
    size = (AVATAR_BOX[2] - AVATAR_BOX[0], AVATAR_BOX[3] - AVATAR_BOX[1])
    if not sources:
        return placeholder_avatar(size), None

    source = rng.choice(sources)
    try:
        with Image.open(source) as image:
            return crop_to_cover(image, size), relative(source)
    except Exception:
        return placeholder_avatar(size), None


def draw_watermark(image: Image.Image) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.text((670, 1530), WATERMARK, font=FONTS["watermark"], fill=(214, 36, 42, 78))
    draw.text((620, 2910), WATERMARK, font=FONTS["watermark"], fill=(214, 36, 42, 78))
    rotated = layer.rotate(-14, resample=Image.Resampling.BICUBIC, center=(1240, 1754))
    image.alpha_composite(rotated)


def draw_wrapped_address(draw: ImageDraw.ImageDraw, address: str) -> None:
    start = 0
    y = 1120
    while start + 11 < len(address):
        draw.text((630, y), address[start : start + 11], fill=(0, 0, 0), font=FONTS["field"])
        start += 11
        y += 100
    draw.text((630, y), address[start:], fill=(0, 0, 0), font=FONTS["field"])


def render_full(front_fields: dict[str, str], back_fields: dict[str, str], avatar: Image.Image) -> Image.Image:
    image = Image.open(GPL_TEMPLATE_PATH).convert("RGBA")
    draw = ImageDraw.Draw(image)
    birth = date.fromisoformat(front_fields["birth"])

    draw.text((630, 690), front_fields["name"], fill=(0, 0, 0), font=FONTS["name"])
    draw.text((630, 840), front_fields["gender"], fill=(0, 0, 0), font=FONTS["field"])
    draw.text((1030, 840), front_fields["nation"], fill=(0, 0, 0), font=FONTS["field"])
    draw.text((630, 980), str(birth.year), fill=(0, 0, 0), font=FONTS["birth"])
    draw.text((950, 980), str(birth.month), fill=(0, 0, 0), font=FONTS["birth"])
    draw.text((1150, 980), str(birth.day), fill=(0, 0, 0), font=FONTS["birth"])
    draw_wrapped_address(draw, front_fields["address"])
    draw.text((950, 1475), front_fields["id_number"], fill=(0, 0, 0), font=FONTS["id"])

    image.alpha_composite(avatar.convert("RGBA"), dest=(AVATAR_BOX[0], AVATAR_BOX[1]))
    draw.text((1050, 2750), back_fields["issue_authority"], fill=(0, 0, 0), font=FONTS["field"])
    draw.text((1050, 2895), back_fields["valid_period"], fill=(0, 0, 0), font=FONTS["field"])
    draw_watermark(image)
    return image.convert("RGB")


def save_jpg(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=QUALITY_JPEG, optimize=True)


def clear_generated_normal_images() -> None:
    for pattern in (
        OUTPUT_DIR / "front" / "normal" / "id_front_*.jpg",
        OUTPUT_DIR / "back" / "normal" / "id_back_*.jpg",
    ):
        for path in pattern.parent.glob(pattern.name):
            path.unlink()


def generate(count: int, seed: int = 20260615) -> list[dict[str, object]]:
    if not GPL_TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Missing GPL template asset: {GPL_TEMPLATE_PATH}")

    rng = random.Random(seed)
    sources = avatar_sources()
    clear_generated_normal_images()
    generated: list[dict[str, object]] = []

    for index in range(1, count + 1):
        front_fields, back_fields = random_fields(rng, index)
        avatar, avatar_source = load_avatar(rng, sources)
        full = render_full(front_fields, back_fields, avatar)

        front_path = OUTPUT_DIR / "front" / "normal" / f"id_front_{index:04d}.jpg"
        back_path = OUTPUT_DIR / "back" / "normal" / f"id_back_{index:04d}.jpg"
        save_jpg(full.crop(FRONT_CROP), front_path)
        save_jpg(full.crop(BACK_CROP), back_path)

        sample_id = f"id_card_{index:04d}"
        generated.append(
            {
                "sample_id": sample_id,
                "image_path": relative(front_path),
                "doc_type": "id_card_front",
                "quality_type": "normal",
                "is_synthetic": True,
                "source": "generated_id_card_gpl_assets",
                "side": "front",
                "fields": deepcopy(front_fields),
                "assets": {
                    "avatar_source": avatar_source,
                    "template_source": relative(GPL_TEMPLATE_PATH),
                    "license": "GPL-3.0 airob0t/idcardgenerator assets",
                },
            }
        )
        generated.append(
            {
                "sample_id": sample_id,
                "image_path": relative(back_path),
                "doc_type": "id_card_back",
                "quality_type": "normal",
                "is_synthetic": True,
                "source": "generated_id_card_gpl_assets",
                "side": "back",
                "fields": deepcopy(back_fields),
                "assets": {
                    "template_source": relative(GPL_TEMPLATE_PATH),
                    "license": "GPL-3.0 airob0t/idcardgenerator assets",
                },
            }
        )

    existing = load_labels()
    preserved = [item for item in existing if item.get("doc_type") not in {"id_card_front", "id_card_back"}]
    write_labels(preserved + generated)
    return generated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate GPL-asset synthetic ID-card normal images.")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260615)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    labels = generate(count=args.count, seed=args.seed)
    print(f"Generated {len(labels)} GPL-asset ID-card normal labels")
    print(f"Front normal dir: {relative(OUTPUT_DIR / 'front' / 'normal')}")
    print(f"Back normal dir: {relative(OUTPUT_DIR / 'back' / 'normal')}")
    print(f"Wrote labels to {relative(LABELS_PATH)}")


if __name__ == "__main__":
    main()
