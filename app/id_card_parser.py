"""ID-card side detection and field parsing utilities."""

from __future__ import annotations

import re


ID_NUMBER_PATTERN = re.compile(
    r"(?<!\d)([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
)
DATE_PATTERN = re.compile(r"((?:19|20)\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})日?")
VALID_PERIOD_PATTERN = re.compile(
    r"((?:19|20)\d{2}[.\-/年]\d{1,2}[.\-/月]\d{1,2}日?)\s*[-至到]\s*((?:19|20)\d{2}[.\-/年]\d{1,2}[.\-/月]\d{1,2}日?|长期)"
)

FRONT_CUES = ("姓名", "性别", "民族", "出生", "住址", "公民身份号码", "身份号码", "身份证号")
BACK_CUES = ("签发机关", "有效期限", "居民身份证", "中华人民共和国", "非真实居民身份证")
FRONT_LABELS = ("姓名", "性别", "民族", "出生", "住址", "公民身份号码", "身份号码", "身份证号")
BACK_LABELS = ("签发机关", "有效期限")


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _clean_line(value: str) -> str:
    return _clean_text(value).strip("：: ")


def _value_after_label(line: str, label: str) -> str | None:
    cleaned = _clean_line(line)
    index = cleaned.find(label)
    if index < 0:
        return None
    value = cleaned[index + len(label) :].lstrip("：:")
    return value or None


def detect_id_card_side(ocr_text: str) -> str:
    normalized = _clean_text(ocr_text)
    front_score = sum(1 for cue in FRONT_CUES if cue in normalized)
    back_score = sum(1 for cue in BACK_CUES if cue in normalized)
    if ID_NUMBER_PATTERN.search(normalized):
        front_score += 2
    if VALID_PERIOD_PATTERN.search(normalized):
        back_score += 2

    if front_score == 0 and back_score == 0:
        return "unknown"
    if front_score >= back_score:
        return "front"
    return "back"


def _extract_name(lines: list[str]) -> str | None:
    for line in lines:
        value = _value_after_label(line, "姓名")
        if value:
            value = re.split(r"性别|民族|出生|住址|公民身份号码|身份号码|身份证号", value)[0]
            return value or None
    return None


def _extract_labeled_value(lines: list[str], label: str, stop_labels: tuple[str, ...]) -> str | None:
    for line in lines:
        value = _value_after_label(line, label)
        if not value:
            continue
        for stop_label in stop_labels:
            stop_index = value.find(stop_label)
            if stop_index >= 0:
                value = value[:stop_index]
        return value or None
    return None


def _extract_birth(ocr_text: str) -> str | None:
    normalized = _clean_text(ocr_text)
    match = re.search(r"出生((?:19|20)\d{2})年?(\d{1,2})月?(\d{1,2})日?", normalized)
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _extract_address(lines: list[str]) -> str | None:
    parts: list[str] = []
    collecting = False
    for line in lines:
        if not collecting:
            value = _value_after_label(line, "住址")
            if not value:
                continue
            parts.append(value)
            collecting = True
            continue

        if any(label in line for label in ("公民身份号码", "身份号码", "身份证号", "签发机关", "有效期限")):
            break
        if any(label in line for label in ("姓名", "性别", "民族", "出生")):
            break
        if line:
            parts.append(line)

    address = "".join(parts)
    return address or None


def _normalize_date(value: str) -> str:
    match = DATE_PATTERN.search(value)
    if not match:
        return value
    year, month, day = match.groups()
    return f"{year}.{int(month):02d}.{int(day):02d}"


def _extract_valid_period(ocr_text: str) -> str | None:
    normalized = _clean_text(ocr_text)
    match = VALID_PERIOD_PATTERN.search(normalized)
    if match:
        start, end = match.groups()
        return f"{_normalize_date(start)}-{_normalize_date(end)}"

    for line in ocr_text.splitlines():
        value = _value_after_label(line, "有效期限")
        if value:
            return value
    return None


def parse_id_card_front_fields(ocr_text: str) -> dict[str, str | None]:
    lines = [_clean_line(line) for line in ocr_text.splitlines() if _clean_line(line)]
    id_match = ID_NUMBER_PATTERN.search(_clean_text(ocr_text))
    return {
        "name": _extract_name(lines),
        "gender": _extract_labeled_value(lines, "性别", ("民族", "出生", "住址")),
        "nation": _extract_labeled_value(lines, "民族", ("出生", "住址")),
        "birth": _extract_birth(ocr_text),
        "address": _extract_address(lines),
        "id_number": id_match.group(1).upper() if id_match else None,
    }


def parse_id_card_back_fields(ocr_text: str) -> dict[str, str | None]:
    lines = [_clean_line(line) for line in ocr_text.splitlines() if _clean_line(line)]
    return {
        "issue_authority": _extract_labeled_value(lines, "签发机关", ("有效期限",)),
        "valid_period": _extract_valid_period(ocr_text),
    }


def parse_id_card_fields(ocr_text: str) -> dict[str, object]:
    side = detect_id_card_side(ocr_text)
    if side == "front":
        fields: dict[str, str | None] = parse_id_card_front_fields(ocr_text)
    elif side == "back":
        fields = parse_id_card_back_fields(ocr_text)
    else:
        fields = {}

    return {
        "side": side,
        "fields": fields,
    }

