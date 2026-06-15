"""Image and OCR quality checks."""

from __future__ import annotations

import cv2
import numpy as np


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
    glare_mask = (value > 245) & (saturation < 45)
    glare_ratio = float(glare_mask.mean())
    return bool(glare_ratio > 0.015)


def check_image_quality(image_path: str) -> dict:
    is_blur = detect_blur(image_path)
    brightness = detect_brightness(image_path)
    has_glare = detect_glare(image_path)
    quality_result = "review" if is_blur or brightness != "normal" or has_glare else "pass"
    return {
        "is_blur": is_blur,
        "brightness": brightness,
        "has_glare": has_glare,
        "quality_result": quality_result,
    }
