"""Generate bank-card abnormal image samples.

Run:
    python scripts/augment_images.py

Only processes data/processed/bank_card/normal/*.png.

强度是**连续随机**的，不是固定常量
----------------------------------
早先的版本把增强强度写死（``GaussianBlur(radius=2.4)``、``enhance(0.55)``），
结果是 100 张 blur 图的 Laplacian 方差全部落在 4.4–4.7 之间（std=0.05）——
它们测的其实是同一张图。

本版本对每个 (样本, 类型) 用**独立派生**的种子采样强度，区间刻意覆盖平台
检测阈值的两侧（``app/quality_check.py``：blur 方差 80、dark 灰度 65、
bright 灰度 210、glare 亮斑占比 0.005）。这样同一类里既有「一眼就糊」的
样本，也有「刚好卡在阈值边上」的边界样本 —— 后者才是能真正检验判定的那批。

派生种子用 ``f"{seed}-{index}-{quality_type}"`` 而不是共用一个 ``rng``：
共享 rng 是顺序消费的，增删任何一个样本都会让后面所有样本的强度错位，
数据集就不可复现了。
"""

from __future__ import annotations

import json
import random
import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter


ROOT_DIR = Path(__file__).resolve().parents[1]
BANK_CARD_DIR = ROOT_DIR / "data" / "processed" / "bank_card"
NORMAL_DIR = BANK_CARD_DIR / "normal"
LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"
QUALITY_TYPES = ("blur", "glare", "occlusion", "rotate", "dark", "bright")

#: 平台阈值，来自 app/quality_check.py。写在这里是为了让采样区间有据可依，
#: 改动它们必须同步 app/quality_check.py，否则数据集就不再覆盖边界。
THRESHOLD_BLUR_VARIANCE = 80.0
THRESHOLD_DARK_MEAN = 65.0
THRESHOLD_BRIGHT_MEAN = 210.0

#: 少数索引必须「明显地」落在异常侧：CI 只保留 0001–0003，
#: 而 tests/test_quality.py 断言这三张分别真的被检出对应退化。
STRONG_INDEXES = (1, 2, 3)

#: 采样区间。低端明显越界、高端贴近甚至越过阈值，制造分层。
BLUR_RADIUS_RANGE = (0.6, 7.0)
BRIGHT_GAIN_RANGE = (1.15, 1.75)
BRIGHT_OFFSET_RANGE = (60, 130)

#: dark 的三档目标灰度。
#:
#: 为什么不再是「一个 enhance 系数」：亮度增强是整体乘法，同一个系数
#: 对亮底图和暗底图的效果完全不同 —— id_card 的纸是 206、bank_card 只有 85，
#: 压到判暗阈值（65）对前者要 ×0.32、对后者只需 ×0.76。
#: 更要命的是「达到阈值」不等于「看不清」：bank_card 压到 68 时字段依然清楚，
#: 于是大部分 dark 样本看起来「还挺清楚」。所以改成按目标灰度反推，
#: 并显式分三档，保证整批覆盖「勉强可读 → 完全不可读」的连续谱。
DARK_TIERS = (
    (52.0, 62.0),   # 轻度：偏暗但仍能勉强辨认字段
    (34.0, 50.0),   # 中度：需要仔细看
    (10.0, 30.0),   # 重度：文字几乎糊掉
)
#: index 1–3 必须稳稳判暗（CI 只留这三张），用中度偏重的一档
STRONG_DARK_TIER = (14.0, 34.0)

#: 亮斑面积占全图的比例区间。平台判据是 `> 0.005`，所以这个区间
#: 必须覆盖它两侧：低端不触发、高端明显触发。
GLARE_AREA_RATIO_RANGE = (0.002, 0.030)

#: index 1–3 用「稳稳越界」的区间，保证 CI 那三张一定被检出。
STRONG_BLUR_RADIUS = (3.5, 6.5)
STRONG_BRIGHT_GAIN = (1.45, 1.75)
STRONG_BRIGHT_OFFSET = (105, 130)


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
    """反光：亮斑面积直接决定平台是否判 glare。

    平台判据是「最大亮斑面积 / 全图面积 > 0.005」，且 mask 要求像素
    ``value > 245 且 saturation < 45``。注意：**半透明白块不满足该条件** ——
    叠加 alpha<255 的白色后，像素值只被抬高一部分，往往够不到 245。
    所以这里直接把椭圆画成不透明白色，并按**面积比例**反推半轴长度，
    让采样区间真的跨过 0.005，而不是靠固定像素尺寸碰运气。
    """
    result = image.convert("RGBA")
    width, height = result.size
    total_area = width * height

    # 目标亮斑面积占全图的比例：低端不触发，高端明显触发
    target_ratio = rng.uniform(*GLARE_AREA_RATIO_RANGE)
    target_area = max(1.0, target_ratio * total_area)
    aspect = rng.uniform(1.4, 2.6)
    # 椭圆面积 = pi * a * b，b = a / aspect
    semi_major = (target_area * aspect / 3.141592653589793) ** 0.5
    semi_minor = semi_major / aspect

    center_x = rng.randint(int(width * 0.30), int(width * 0.75))
    center_y = rng.randint(int(height * 0.20), int(height * 0.65))
    angle = rng.uniform(-24, 24)

    patch_size = int(max(semi_major, semi_minor) * 4) + 20
    patch = Image.new("RGBA", (patch_size, patch_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(patch)
    cx = cy = patch_size // 2
    # 外圈用半透明做柔和过渡（不参与 mask），内核用纯白（参与 mask）
    for step in range(12, 1, -1):
        ratio = step / 12
        rx = max(1, int(semi_major * ratio))
        ry = max(1, int(semi_minor * ratio))
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(255, 255, 255, 120))
    draw.ellipse((cx - semi_major, cy - semi_minor, cx + semi_major, cy + semi_minor), fill=(255, 255, 255, 255))

    patch = patch.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    result.alpha_composite(patch, (center_x - patch.size[0] // 2, center_y - patch.size[1] // 2))
    return result.convert("RGB")


def add_occlusion(image: Image.Image, rng: random.Random) -> Image.Image:
    """遮挡：位置和大小都随机（平台不检测这类，它影响的是字段层）。"""
    result = image.copy()
    draw = ImageDraw.Draw(result)
    width, height = result.size
    box_w = rng.randint(int(width * 0.20), int(width * 0.40))
    box_h = rng.randint(int(height * 0.09), int(height * 0.16))
    x0 = rng.randint(int(width * 0.20), max(int(width * 0.21), width - box_w - 30))
    y0 = rng.randint(int(height * 0.30), max(int(height * 0.31), height - box_h - 40))
    draw.rounded_rectangle((x0, y0, x0 + box_w, y0 + box_h), radius=6, fill=(44, 48, 54))
    return result


def rotate(image: Image.Image, rng: random.Random) -> Image.Image:
    angle = rng.uniform(-8, 8)
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=(20, 35, 52))


def gray_mean(image: Image.Image) -> float:
    return float(np.asarray(image.convert("L"), dtype=np.float32).mean())


def darken_to_target(image: Image.Image, rng: random.Random, *, strong: bool = False) -> Image.Image:
    """按目标灰度压暗，而不是套一个固定的 enhance 系数。

    先量当前灰度再反推系数，这样亮底（id_card 纸 206）和暗底（bank_card 纸 85）
    都能真正落到目标区间；三档轮转保证整批覆盖「勉强可读 → 完全不可读」。
    """
    if strong:
        low, high = STRONG_DARK_TIER
    else:
        low, high = DARK_TIERS[rng.randrange(len(DARK_TIERS))]
    target = rng.uniform(low, high)
    current = gray_mean(image)
    factor = 1.0 if current <= 1 else target / current
    factor = max(0.02, min(1.0, factor))
    return ImageEnhance.Brightness(image).enhance(factor)


def augment_image(
    image: Image.Image,
    quality_type: str,
    rng: random.Random,
    *,
    strong: bool = False,
) -> Image.Image:
    """按类型施加退化。``strong`` 时用「稳越界」区间，供 CI 保留的样本使用。"""
    if quality_type == "blur":
        low, high = STRONG_BLUR_RADIUS if strong else BLUR_RADIUS_RANGE
        return image.filter(ImageFilter.GaussianBlur(radius=rng.uniform(low, high)))

    if quality_type == "glare":
        return add_glare(image, rng)

    if quality_type == "occlusion":
        return add_occlusion(image, rng)

    if quality_type == "rotate":
        return rotate(image, rng)

    if quality_type == "dark":
        return darken_to_target(image, rng, strong=strong)

    if quality_type == "bright":
        gain_low, gain_high = STRONG_BRIGHT_GAIN if strong else BRIGHT_GAIN_RANGE
        offset_low, offset_high = STRONG_BRIGHT_OFFSET if strong else BRIGHT_OFFSET_RANGE
        gain = rng.uniform(gain_low, gain_high)
        offset = rng.uniform(offset_low, offset_high)
        return image.point(lambda value: min(255, int(value * gain + offset)))

    raise ValueError(f"Unsupported quality type: {quality_type}")


def normal_bank_labels(labels: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    rows = {}
    for item in labels:
        if item.get("doc_type") == "bank_card" and item.get("quality_type") == "normal":
            rows[str(item["image_path"])] = item
    return rows


def augment(quality_types: tuple[str, ...] = QUALITY_TYPES, seed: int = 20260615) -> list[dict[str, object]]:
    labels = read_labels()
    normal_labels = normal_bank_labels(labels)
    generated: list[dict[str, object]] = []

    for source_path in sorted(NORMAL_DIR.glob("*.png")):
        source_rel = relative(source_path)
        source_label = normal_labels.get(source_rel)
        if not source_label:
            continue

        index = parse_index(source_path.stem)
        with Image.open(source_path) as handle:
            image = handle.convert("RGB")

        for quality_type in quality_types:
            output_dir = BANK_CARD_DIR / quality_type
            output_path = output_dir / source_path.name
            output_dir.mkdir(parents=True, exist_ok=True)

            # 每个 (样本, 类型) 独立派生种子 —— 见模块 docstring
            rng = random.Random(f"{seed}-{source_path.stem}-{quality_type}")
            strong = index in STRONG_INDEXES
            augment_image(image, quality_type, rng, strong=strong).save(output_path)

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


def parse_index(stem: str) -> int:
    try:
        return int(stem.rsplit("_", 1)[-1])
    except ValueError:
        return -1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate bank-card abnormal image samples.")
    parser.add_argument(
        "--types",
        nargs="+",
        choices=QUALITY_TYPES,
        default=list(QUALITY_TYPES),
        help="Quality types to regenerate. Defaults to all abnormal types.",
    )
    parser.add_argument("--seed", type=int, default=20260615)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated = augment(tuple(args.types), seed=args.seed)
    print(f"Generated {len(generated)} augmented bank card images")
    print(f"Wrote labels to {relative(LABELS_PATH)}")


if __name__ == "__main__":
    main()
