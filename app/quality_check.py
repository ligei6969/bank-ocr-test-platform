"""Image and OCR quality checks."""

from __future__ import annotations

import cv2
import numpy as np


GLARE_VALUE_THRESHOLD = 245
GLARE_SATURATION_THRESHOLD = 45
# Chosen from current synthetic normal/glare component-ratio distributions.
GLARE_COMPONENT_RATIO_THRESHOLD = 0.005

BLUR_VARIANCE_THRESHOLD = 80.0
BRIGHTNESS_DARK_THRESHOLD = 65
BRIGHTNESS_BRIGHT_THRESHOLD = 210

#: 严重退化阈值 —— 命中即直接拒绝，而不是转人工。
#: 标定自 40 条带人工结论的 golden 样本（见 ``_severe_reasons``）。
SEVERE_BLUR_VARIANCE_THRESHOLD = 30.0
SEVERE_BRIGHTNESS_DARK_THRESHOLD = 35.0
SEVERE_BRIGHTNESS_BRIGHT_THRESHOLD = 215.0

#: 反光的严重度阈值：**当前停用**（``None``）。
#:
#: 留在这里而不是删掉，是因为那个标定值（0.022）本身是有效的，只是它的
#: 支撑太薄：最高 review 样本 0.0213 与最低 reject 样本 0.0232 只差 9%，
#: 这个间隙换一批样本几乎必然失效。按它判 reject 会得到一个看着精确、
#: 实则靠运气的口径 —— 与其如此，宁可让反光一律走人工复核。
#: 待样本量足够（或改用「反光是否压住关键字段」这类与面积无关的判据）
#: 再设回具体数值。
SEVERE_GLARE_COMPONENT_RATIO_THRESHOLD: float | None = None


def _severe_reasons(metrics: dict[str, float]) -> list[str]:
    """Return reason codes for degradations too severe to repair by re-review.

    这些阈值是在 40 条 golden 标注样本上标定的（人工结论要求：极端退化直接
    拒绝，而非转人工），不是行业标准 —— 换一批样本应重新标定。

    反光轴当前停用，见 ``SEVERE_GLARE_COMPONENT_RATIO_THRESHOLD`` 的说明。
    """
    reasons: list[str] = []
    variance = metrics.get("blur_laplacian_variance")
    brightness = metrics.get("brightness_mean")
    glare_ratio = metrics.get("glare_component_ratio")

    if variance is not None and variance < SEVERE_BLUR_VARIANCE_THRESHOLD:
        reasons.append("severe_image_blur")
    if brightness is not None and brightness < SEVERE_BRIGHTNESS_DARK_THRESHOLD:
        reasons.append("severe_image_dark")
    if brightness is not None and brightness > SEVERE_BRIGHTNESS_BRIGHT_THRESHOLD:
        reasons.append("severe_image_bright")
    if (
        SEVERE_GLARE_COMPONENT_RATIO_THRESHOLD is not None
        and glare_ratio is not None
        and glare_ratio > SEVERE_GLARE_COMPONENT_RATIO_THRESHOLD
    ):
        reasons.append("severe_glare_detected")
    return reasons


def get_quality_reasons(quality: dict) -> list[str]:
    """Return stable reason codes for an image quality result."""
    existing_reasons = quality.get("quality_reasons")
    if isinstance(existing_reasons, list):
        return [str(reason) for reason in existing_reasons]

    reasons: list[str] = []
    if quality.get("is_blur"):
        reasons.append("image_blur")
    brightness = quality.get("brightness")
    if brightness == "dark":
        reasons.append("image_dark")
    elif brightness == "bright":
        reasons.append("image_bright")
    if quality.get("has_glare"):
        reasons.append("glare_detected")
    return reasons


def _read_image(image_path: str) -> np.ndarray:
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Unable to read image: {image_path}")
    return image


def _blur_variance(image_path: str) -> float:
    image = _read_image(image_path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _brightness_mean(image_path: str) -> float:
    image = _read_image(image_path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(gray.mean())


def _glare_component_ratio(image_path: str) -> float:
    image = _read_image(image_path)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    value = hsv[:, :, 2]
    saturation = hsv[:, :, 1]
    glare_mask = (value > GLARE_VALUE_THRESHOLD) & (saturation < GLARE_SATURATION_THRESHOLD)
    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(glare_mask.astype("uint8"), 8)
    if component_count <= 1:
        return 0.0
    largest_area = int(stats[1:, cv2.CC_STAT_AREA].max())
    return largest_area / glare_mask.size


def detect_blur(image_path: str) -> bool:
    return bool(_blur_variance(image_path) < BLUR_VARIANCE_THRESHOLD)


def detect_brightness(image_path: str) -> str:
    return _brightness_label(_brightness_mean(image_path))


def _brightness_label(mean_value: float) -> str:
    if mean_value < BRIGHTNESS_DARK_THRESHOLD:
        return "dark"
    if mean_value > BRIGHTNESS_BRIGHT_THRESHOLD:
        return "bright"
    return "normal"


def detect_glare(image_path: str) -> bool:
    return bool(_glare_component_ratio(image_path) > GLARE_COMPONENT_RATIO_THRESHOLD)


def measure_image_quality(image_path: str) -> dict[str, float]:
    """一次读图算出全部原始指标。

    ``check_image_quality`` 需要这些数值来判严重度，但旧实现只把它们换算成
    布尔值就丢掉。字段名与 ``ai_service/thresholds.py`` 的 ``METRIC_*`` 常量
    一致，这样平台 payload 一旦带上它们，AI 服务就能从较弱的 ``flags`` 模式
    切到 ``metrics`` 模式重算质量。
    """
    mean_value = _brightness_mean(image_path)
    return {
        "blur_laplacian_variance": _blur_variance(image_path),
        "brightness_mean": mean_value,
        "glare_component_ratio": _glare_component_ratio(image_path),
    }


def check_image_quality(image_path: str) -> dict:
    metrics = measure_image_quality(image_path)
    brightness = _brightness_label(metrics["brightness_mean"])
    is_blur = metrics["blur_laplacian_variance"] < BLUR_VARIANCE_THRESHOLD
    has_glare = metrics["glare_component_ratio"] > GLARE_COMPONENT_RATIO_THRESHOLD
    quality_result = "review" if is_blur or brightness != "normal" or has_glare else "pass"
    result = {
        "is_blur": is_blur,
        "brightness": brightness,
        "has_glare": has_glare,
        "quality_result": quality_result,
    }
    result["quality_reasons"] = get_quality_reasons(result)
    result["quality_metrics"] = metrics
    result["severe_reasons"] = _severe_reasons(metrics)
    return result
