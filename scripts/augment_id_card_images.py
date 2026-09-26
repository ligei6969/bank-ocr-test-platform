"""Augment synthetic ID-card normal images into abnormal quality samples.

Run:
    python scripts/augment_id_card_images.py

强度是**连续随机**的，理由与 ``scripts/augment_images.py`` 相同：
固定强度会让同类样本退化成同一张图，测不出判定的边界。
每个 (样本, 类型) 用独立派生的种子，保证增删样本不影响其余样本的强度。
"""

from __future__ import annotations

import argparse
import json
import random
from copy import deepcopy
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

try:
    import cv2

    _HAS_CV2 = True
except ImportError:  # 项目依赖里有 cv2，但脚本自身不该因此跑不起来
    _HAS_CV2 = False


ROOT_DIR = Path(__file__).resolve().parents[1]
ID_CARD_DIR = ROOT_DIR / "data" / "processed" / "id_card"
LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"
QUALITY_TYPES = ("blur", "glare", "occlusion", "rotate", "dark", "bright")
SIDES = ("front", "back")

#: 与 bank_card 侧同约定：这几个索引用「稳越界」的高档强度，
#: 保证它们一定表现出标注的退化。id_card 不被 CI 裁剪，但保持一致便于对照。
STRONG_INDEXES = (1, 2, 3)

#: 平台阈值，来自 app/quality_check.py。
#: 亮度类增强必须**按目标灰度反推系数**，不能用绝对系数：
#: id_card 底图灰度约 206–209，bank_card 只有 85。
#: 同一个 enhance(0.55) 对前者是 113（仍判正常），对后者才是 47（判暗）。
THRESHOLD_DARK_MEAN = 65.0
THRESHOLD_BRIGHT_MEAN = 210.0
THRESHOLD_BLUR_VARIANCE = 80.0
THRESHOLD_GLARE_RATIO = 0.005

#: 目标灰度区间。dark 要落在阈值**下方**，bright 落在**上方**，
#: 并各自留一段「贴近阈值」的边界样本。
#:
#: dark 分三档，而不是一个连续区间：只要求「低于 65」会得到一大批
#: 「虽然偏暗但字段照样看得清」的样本（实测 bank_card 有 21/100 落在 65–80），
#: 那样测不出「暗到什么程度开始不可读」。三档保证从勉强可读到几乎不可读都有。
DARK_TIERS = (
    (50.0, 62.0),   # 轻度：偏暗但字段勉强可辨
    (32.0, 48.0),   # 中度：需要仔细辨认
    (10.0, 28.0),   # 重度：文字几乎糊掉
)
BRIGHT_TARGET_MEAN = (196.0, 252.0)  # 下限略低于 210，制造边界样本

#: 模糊半径分三档。id_card 底图（正面方差 259–447、背面 635–688）比
#: bank_card（873–1521）清晰得多，所以同一个半径对 id_card 的压制更弱：
#: 旧区间上限 7.0 时，实测仍有样本方差高于 80 的判定线。
#: 三档保证从「轻微发虚」到「完全糊掉」都有，且高档足够压到判据以下。
BLUR_RADIUS_TIERS = (
    (1.2, 2.4),    # 轻度：轻微发虚
    (2.8, 4.6),    # 中度：明显模糊
    (5.5, 10.0),   # 重度：文字糊成一团
)
STRONG_BLUR_RADIUS = (6.5, 10.0)

#: 亮斑面积比例分三档。判据是 > 0.005，所以三档要跨过它。
#: 旧区间上限 0.030 偏小，实测中位仅 0.015，看着「不太反光」。
GLARE_AREA_RATIO_TIERS = (
    (0.002, 0.006),   # 轻度：刚过判据
    (0.010, 0.028),   # 中度：明显一片反光
    (0.040, 0.090),   # 重度：大面积过曝
)
STRONG_GLARE_RATIO = (0.045, 0.090)

#: 遮挡块的面积占比与块数。旧实现只有一块、占宽 18–34%，
#: 实测 edge 与 normal 完全重叠（3.70–6.78 vs 3.62–6.67），等于没遮挡。
#: 改成多块 + 更大面积，并让高档直接压住字段区域。
OCCLUSION_TIERS = (
    ((0.22, 0.36), 1),   # 轻度：一块小遮挡
    ((0.38, 0.58), 2),   # 中度：两块
    ((0.62, 0.85), 3),   # 重度：三块大面积
)
STRONG_OCCLUSION = ((0.65, 0.90), 3)

#: 旋转角度分档。旧区间 -8..8 度人眼几乎无感，实测 edge 变化很小。
ROTATE_ANGLE_TIERS = (
    (-6.0, 6.0),     # 轻度
    (-14.0, -8.0),   # 中度左倾
    (8.0, 16.0),     # 中度右倾
)
STRONG_ROTATE_ANGLE = (10.0, 18.0)


def relative(path: Path) -> str:
    return path.relative_to(ROOT_DIR).as_posix()


def parse_index(stem: str) -> int:
    try:
        return int(stem.rsplit("_", 1)[-1])
    except ValueError:
        return -1


def load_labels() -> list[dict[str, object]]:
    if not LABELS_PATH.exists():
        return []
    return json.loads(LABELS_PATH.read_text(encoding="utf-8"))


def write_labels(labels: list[dict[str, object]]) -> None:
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LABELS_PATH.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")


def gray_mean(image: Image.Image) -> float:
    return float(np.asarray(image.convert("L"), dtype=np.float32).mean())


def measure_glare_ratio(image: Image.Image) -> float:
    """最大亮斑面积 / 全图面积，与 platform 的判据同口径。

    用 numpy 直接算 HSV 阈值下的连通域近似 —— 这里只需要「底图自带多少高光」
    这个量级，不需要 cv2 级别的精确连通域。
    """
    arr = np.asarray(image.convert("RGB"), dtype=np.float32)
    value = arr.max(axis=2)
    saturation = np.where(value > 0, (value - arr.min(axis=2)) / np.maximum(value, 1) * 255, 0)
    mask = (value > 245) & (saturation < 45)
    return float(mask.sum()) / mask.size


def enhance_to_target_mean(image: Image.Image, rng: random.Random, target_range: tuple[float, float]) -> Image.Image:
    """按「目标灰度」反推亮度系数，而不是用绝对系数。

    绝对系数对两个 surface 是错的：id_card 底图 206、bank_card 85，
    同一个 0.55 一个仍在正常区、一个已经判暗。所以这里先量当前灰度，
    再算出让结果落进 ``target_range`` 的系数。
    """
    current = gray_mean(image)
    target = rng.uniform(*target_range)
    if current <= 1:
        factor = 1.0
    else:
        factor = target / current
    # 系数过大会把图压成纯黑、过小则看不出变化，都夹到合理范围
    factor = max(0.02, min(4.0, factor))
    return ImageEnhance.Brightness(image).enhance(factor)


def text_anchor(image: Image.Image, rng: random.Random) -> tuple[int, int]:
    """找一个「信息密集」的坐标，用来摆放遮挡块。

    纯随机摆放在实测里 90% 的样本把黑块盖到了空白处（遮挡区域只有 <30%
    落在文字上），等于没遮住任何字段。这里用高斯拉普拉斯响应定位文字/纹理，
    从响应最强的那些像素里随机挑一个作为锚点，让遮挡真正压住内容。
    """
    gray_u8 = np.asarray(image.convert("L"), dtype=np.uint8)
    if _HAS_CV2:
        detail = np.abs(cv2.Laplacian(gray_u8, cv2.CV_64F))
    else:
        detail = _approx_detail(gray_u8.astype(np.float32))
    if detail.size == 0:
        h, w = gray_u8.shape
        return rng.randint(0, w - 1), rng.randint(0, h - 1)

    # 取响应最强的前 15% 像素作为候选
    thr = np.percentile(detail, 85)
    ys, xs = np.where(detail >= thr)
    if len(xs) == 0:
        h, w = gray_u8.shape
        return rng.randint(0, w - 1), rng.randint(0, h - 1)
    pick = rng.randrange(len(xs))
    return int(xs[pick]), int(ys[pick])


def _approx_detail(gray: np.ndarray) -> np.ndarray:
    """无 cv2 时的退化实现：用相邻像素差近似细节强度。"""
    gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1]))
    gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    return gy + gx


def add_occlusion(image: Image.Image, rng: random.Random, *, strong: bool = False) -> Image.Image:
    """盖住一块或多块内容，锚点落在文字密集处。

    旧实现只画一块、且只占宽度 18–34%，实测 edge 值与 normal 完全重叠；
    更关键的是随机摆放导致 90% 的遮挡盖在留白上，等于没有遮挡。
    这里改成：面积分档 + 块数随档位增加 + 锚定到纹理密集区。
    """
    result = image.copy()
    draw = ImageDraw.Draw(result)
    w, h = result.size

    if strong:
        (low, high), blocks = STRONG_OCCLUSION
    else:
        (low, high), blocks = OCCLUSION_TIERS[rng.randrange(len(OCCLUSION_TIERS))]

    for _ in range(blocks):
        box_w = max(20, min(int(w * rng.uniform(low, high)), w - 20))
        box_h = max(16, min(int(h * rng.uniform(low * 0.35, high * 0.55)), h - 20))
        cx, cy = text_anchor(image, rng)
        # 以文字锚点为中心摆放，再夹回画布内
        x0 = max(0, min(cx - box_w // 2, w - box_w))
        y0 = max(0, min(cy - box_h // 2, h - box_h))
        draw.rounded_rectangle((x0, y0, x0 + box_w, y0 + box_h), radius=8, fill=(35, 41, 48))
    return result


def rotate_image(image: Image.Image, rng: random.Random, *, strong: bool = False) -> Image.Image:
    """旋转。旧区间 ±8 度人眼几乎无感，实测 edge 变化也很小，这里按档放大。"""
    if strong:
        low, high = STRONG_ROTATE_ANGLE
        angle = rng.uniform(low, high) * rng.choice((-1, 1))
    else:
        low, high = ROTATE_ANGLE_TIERS[rng.randrange(len(ROTATE_ANGLE_TIERS))]
        angle = rng.uniform(low, high)
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=(222, 226, 221))


def add_glare(image: Image.Image, rng: random.Random, *, strong: bool = False) -> Image.Image:
    """反光，亮斑面积按全图比例采样，跨过平台的 0.005 判据。

    注意 mask 要求像素 ``value > 245 且 saturation < 45``：半透明白块达不到
    这个值，只有不透明部分才算数，所以亮斑主体必须画成纯白。
    """
    result = image.convert("RGBA")
    overlay = Image.new("RGBA", result.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = result.size

    if strong:
        low, high = STRONG_GLARE_RATIO
    else:
        low, high = GLARE_AREA_RATIO_TIERS[rng.randrange(len(GLARE_AREA_RATIO_TIERS))]
    target_ratio = rng.uniform(low, high)
    target_area = max(1.0, target_ratio * w * h)
    aspect = rng.uniform(1.3, 2.2)
    semi_major = (target_area * aspect / 3.141592653589793) ** 0.5
    semi_minor = semi_major / aspect

    cx = rng.randint(int(w * 0.30), int(w * 0.80))
    cy = rng.randint(int(h * 0.02), int(h * 0.50))
    # 外圈半透明做过渡（不参与 mask），主体纯白（参与 mask）
    for step in range(10, 1, -1):
        ratio = step / 10
        rx = max(1, int(semi_major * ratio))
        ry = max(1, int(semi_minor * ratio))
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(255, 255, 255, 120))
    draw.ellipse(
        (cx - semi_major, cy - semi_minor, cx + semi_major, cy + semi_minor),
        fill=(255, 255, 255, 255),
    )
    result.alpha_composite(overlay)
    return result.convert("RGB")


def augment_image(
    image: Image.Image,
    quality_type: str,
    rng: random.Random,
    *,
    strong: bool = False,
) -> Image.Image:
    if quality_type == "blur":
        low, high = STRONG_BLUR_RADIUS if strong else BLUR_RADIUS_TIERS[rng.randrange(len(BLUR_RADIUS_TIERS))]
        return image.filter(ImageFilter.GaussianBlur(radius=rng.uniform(low, high)))
    if quality_type == "glare":
        return add_glare(image, rng, strong=strong)
    if quality_type == "occlusion":
        return add_occlusion(image, rng, strong=strong)
    if quality_type == "rotate":
        return rotate_image(image, rng, strong=strong)
    if quality_type == "dark":
        low, high = DARK_TIERS[rng.randrange(len(DARK_TIERS))]
        return enhance_to_target_mean(image, rng, (low, high))
    if quality_type == "bright":
        return enhance_to_target_mean(image, rng, BRIGHT_TARGET_MEAN)
    raise ValueError(f"Unsupported quality type: {quality_type}")


def normal_label_map(labels: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for item in labels:
        if item.get("doc_type") in {"id_card_front", "id_card_back"} and item.get("quality_type") == "normal":
            path = item.get("image_path")
            if isinstance(path, str):
                rows[path] = item
    return rows


def clear_augmented_images(quality_types: tuple[str, ...] = QUALITY_TYPES) -> None:
    for side in SIDES:
        for quality_type in quality_types:
            output_dir = ID_CARD_DIR / side / quality_type
            if not output_dir.exists():
                continue
            for path in output_dir.glob("id_*.jpg"):
                path.unlink()


def augment(
    quality_types: tuple[str, ...] = QUALITY_TYPES,
    seed: int = 20260615,
) -> list[dict[str, object]]:
    labels = load_labels()
    source_labels = normal_label_map(labels)
    generated: list[dict[str, object]] = []
    clear_augmented_images(quality_types)

    for side in SIDES:
        normal_dir = ID_CARD_DIR / side / "normal"
        for source_path in sorted(normal_dir.glob("*.jpg")):
            source_rel = relative(source_path)
            source_label = source_labels.get(source_rel)
            if not source_label:
                continue

            with Image.open(source_path) as image:
                base = image.convert("RGB")
            for quality_type in quality_types:
                output_dir = ID_CARD_DIR / side / quality_type
                output_path = output_dir / source_path.name
                output_dir.mkdir(parents=True, exist_ok=True)

                # 每个 (样本, 类型) 独立派生种子 —— 见模块 docstring
                rng = random.Random(f"{seed}-{side}-{source_path.stem}-{quality_type}")
                strong = parse_index(source_path.stem) in STRONG_INDEXES
                augment_image(base, quality_type, rng, strong=strong).save(
                    output_path, format="JPEG", quality=95, optimize=True
                )

                new_label = deepcopy(source_label)
                new_label["image_path"] = relative(output_path)
                new_label["quality_type"] = quality_type
                new_label["source"] = "augmented_from_normal"
                new_label.setdefault("sample_id", f"id_card_{side}_{source_path.stem}")
                generated.append(new_label)

    preserved = [
        item
        for item in labels
        if not (
            item.get("doc_type") in {"id_card_front", "id_card_back"}
            and item.get("quality_type") in quality_types
        )
    ]
    write_labels(preserved + generated)
    return generated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate ID-card abnormal samples.")
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
    print(f"Generated {len(generated)} augmented ID-card images")
    print(f"Wrote labels to {relative(LABELS_PATH)}")


if __name__ == "__main__":
    main()
