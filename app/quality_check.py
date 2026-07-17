"""Image and OCR quality checks."""

from __future__ import annotations

import cv2
import numpy as np


GLARE_VALUE_THRESHOLD = 245
GLARE_SATURATION_THRESHOLD = 45
# Chosen from current synthetic normal/glare component-ratio distributions.
GLARE_COMPONENT_RATIO_THRESHOLD = 0.005


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


def detect_blur(image_path: str) -> bool:
    image = _read_image(image_path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    variance = cv2.Laplacian(gray, cv2.CV_64F).var()
    return bool(variance < 80.0)


def detect_brightness(image_path: str) -> str:
    image = _read_image(image_path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mean_value = float(gray.mean())
    if mean_value < 65:
        return "dark"
    if mean_value > 210:
        return "bright"
    return "normal"


def detect_glare(image_path: str) -> bool:
    image = _read_image(image_path)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    value = hsv[:, :, 2]
    saturation = hsv[:, :, 1]
    glare_mask = (value > GLARE_VALUE_THRESHOLD) & (saturation < GLARE_SATURATION_THRESHOLD)
    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(glare_mask.astype("uint8"), 8)
    if component_count <= 1:
        return False
    largest_area = int(stats[1:, cv2.CC_STAT_AREA].max())
    largest_component_ratio = largest_area / glare_mask.size
    return bool(largest_component_ratio > GLARE_COMPONENT_RATIO_THRESHOLD)


def check_image_quality(image_path: str) -> dict:
    is_blur = detect_blur(image_path)
    brightness = detect_brightness(image_path)
    has_glare = detect_glare(image_path)
    quality_result = "review" if is_blur or brightness != "normal" or has_glare else "pass"
    result = {
        "is_blur": is_blur,
        "brightness": brightness,
        "has_glare": has_glare,
        "quality_result": quality_result,
    }
    result["quality_reasons"] = get_quality_reasons(result)
    return result
