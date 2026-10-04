"""Conservative image evidence for text tilt and solid dark obstructions.

These heuristics request manual review; they do not identify arbitrary objects
or certify that an image is unobstructed. No filenames or annotations are used.
"""

from __future__ import annotations

import cv2
import numpy as np


ROTATION_REVIEW_THRESHOLD = 1.0
OCCLUSION_AREA_THRESHOLD = 0.008
MAX_ANALYSIS_EDGE = 1000


def _text_line_angle(image: np.ndarray) -> tuple[float, int]:
    """Estimate tilt from agreeing text regions spread over the image."""
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    detail = cv2.max(
        cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel),
        cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel),
    )
    mask = (detail > 25).astype("uint8") * 255
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(width * 0.025)), 3)),
    )
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    angles: list[float] = []
    positions: list[float] = []
    for contour in contours:
        (_cx, cy), (rw, rh), angle = cv2.minAreaRect(contour)
        long_edge, short_edge = max(rw, rh), min(rw, rh)
        if not height * 0.03 < cy < height * 0.97:
            continue
        if (long_edge < width * 0.1 or short_edge < 4 or
                short_edge > height * 0.13 or long_edge / short_edge < 2.5):
            continue
        if rw < rh:
            angle += 90
        angle = (angle + 90) % 180 - 90
        if abs(angle) <= 30:
            angles.append(angle)
            positions.append(cy)
    if len(angles) < 2:
        return 0.0, 0
    median = float(np.median(angles))
    agreeing = [angle for angle in angles if abs(angle - median) <= 1.5]
    rows = [cy for angle, cy in zip(angles, positions) if abs(angle - median) <= 1.5]
    if len(agreeing) < 2 or len(agreeing) < len(angles) * 0.6 or max(rows) - min(rows) < height * 0.15:
        return 0.0, 0
    return float(np.median(agreeing)), len(agreeing)


def _occlusion_area_ratio(image: np.ndarray) -> float:
    """Find large, flat, dark rectangular patches with surrounding contrast.

    A suspected patch is deliberately routed to review, since a legitimate
    dark graphic can look similar. Hands and textured objects need a detector.
    """
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    kernel = np.ones((3, 3), dtype="uint8")
    local_range = np.max(
        cv2.dilate(image, kernel).astype("int16") - cv2.erode(image, kernel), axis=2,
    )
    mask = ((hsv[:, :, 2] < 70) & (hsv[:, :, 1] < 90) & (local_range < 2)).astype("uint8")
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    largest = 0.0
    for index in range(1, count):
        x, y, box_width, box_height, area = map(int, stats[index])
        ratio = area / (width * height)
        if (ratio < OCCLUSION_AREA_THRESHOLD or area / (box_width * box_height) < 0.88 or
                box_width < width * 0.1 or box_height < height * 0.035 or
                box_height > height * 0.85):
            continue
        touches_border = (x < width * 0.02 or y < height * 0.03 or
                          x + box_width > width * 0.98 or y + box_height > height * 0.97)
        if touches_border and not 0.10 <= ratio <= 0.70:
            continue
        pixels = gray[y:y + box_height, x:x + box_width][
            labels[y:y + box_height, x:x + box_width] == index
        ]
        if float(pixels.std()) > 5:
            continue
        padding = max(3, int(width * 0.02))
        x0, y0 = max(0, x - padding), max(0, y - padding)
        x1, y1 = min(width, x + box_width + padding), min(height, y + box_height + padding)
        ring = gray[y0:y1, x0:x1]
        outside = np.ones(ring.shape, dtype=bool)
        outside[y - y0:y + box_height - y0, x - x0:x + box_width - x0] = False
        surrounding_color = np.median(image[y0:y1, x0:x1][outside], axis=0)
        patch_color = np.median(image[y:y + box_height, x:x + box_width][
            labels[y:y + box_height, x:x + box_width] == index
        ], axis=0)
        if float(np.linalg.norm(surrounding_color - patch_color)) >= 6:
            largest = max(largest, ratio)
    return largest


def measure_image_geometry(image: np.ndarray) -> dict[str, float]:
    height, width = image.shape[:2]
    if max(height, width) > MAX_ANALYSIS_EDGE:
        scale = MAX_ANALYSIS_EDGE / max(height, width)
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    angle, support = _text_line_angle(image)
    sideways_angle, sideways_support = _text_line_angle(cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE))
    if sideways_support > support:
        angle = sideways_angle - 90
    return {
        "rotation_angle_degrees": angle,
        "occlusion_area_ratio": _occlusion_area_ratio(image),
    }
