"""ID-card side detection and field parsing utilities.

关于「标签与值分行」
--------------------
真实 PaddleOCR 把证件上的「姓名」和它的值检测成**两个独立的文本框**：

    姓名            ← 一个 box
    沈梓欣          ← 另一个 box

而 mock OCR（``MOCK_OCR_TEXT``）把整段拼成一行 ``姓名 沈梓欣``。
早先的实现只认后者：``_value_after_label`` 在单行内查找标签，
标签后没有内容就返回 ``None``。于是真实 OCR 下身份证字段
**一个都解析不出来**（CTE-2 的快照实测：正面 id_number 0/10，反面全字段 3/15）。

这个假设此前从未被检验，因为**实现与测试都建立在 mock 的输出形状上** ——
单测里的用例也照抄「标签 值」同行。两边一起错，就测不出差异。

本模块现在同时支持两种形状：标签后若本行有值就用本行，没有就看下一行。
"""

from __future__ import annotations

import re


#: 身份证号。
#:
#: **不能用 ``[1-9]`` 打头。** 真实证件号的行政区划码可能是前导零
#: （合成集里就是 ``00000019900831001X``），早先要求首位非零，
#: 于是这些号码全部匹配失败 —— 表现和「OCR 没认出来」一模一样，
#: 很容易归错因。18 位 + 出生日期合法 + 校验位是数字或 X，就够窄了。
ID_NUMBER_PATTERN = re.compile(
    r"(?<!\d)(\d{6}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
)
DATE_PATTERN = re.compile(r"((?:19|20)\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})日?")
VALID_PERIOD_PATTERN = re.compile(
    r"((?:19|20)\d{2}[.\-/年]\d{1,2}[.\-/月]\d{1,2}日?)\s*[-至到]\s*((?:19|20)\d{2}[.\-/年]\d{1,2}[.\-/月]\d{1,2}日?|长期)"
)

FRONT_CUES = ("姓名", "性别", "民族", "出生", "住址", "公民身份号码", "身份号码", "身份证号")
BACK_CUES = ("签发机关", "有效期限", "居民身份证", "中华人民共和国", "非真实居民身份证")
FRONT_LABELS = ("姓名", "性别", "民族", "出生", "住址", "公民身份号码", "身份号码", "身份证号")
BACK_LABELS = ("签发机关", "有效期限")

#: 所有字段标签。跨行取值时要靠它判断「下一行是不是另一个标签」，
#: 免得把「出生」的值吃成上一个字段的内容。
ALL_LABELS = FRONT_LABELS + BACK_LABELS


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _clean_line(value: str) -> str:
    return _clean_text(value).strip("：: ")


def _value_after_label(line: str, label: str) -> str | None:
    """在**本行内**取标签后面的值。取不到返回 ``None``。"""
    cleaned = _clean_line(line)
    index = cleaned.find(label)
    if index < 0:
        return None
    value = cleaned[index + len(label) :].lstrip("：:")
    return value or None


def _looks_like_a_label(line: str) -> bool:
    """这一行是不是「只有标签」或「标签+值」的标签行。

    用于跨行取值时决定「下一行是值还是下一个字段」。
    """
    cleaned = _clean_line(line)
    if not cleaned:
        return False
    return any(cleaned.startswith(label) for label in ALL_LABELS)


def _value_for_label(lines: list[str], label: str) -> str | None:
    """跨行取标签的值。**先看本行，本行没有就用下一行。**

    为什么是「下一行」而不是「往后找第一段不含标签的文字」：
    真实 OCR 的阅读顺序基本稳定（标签在上、值在下），只取紧邻的一行
    已经够用，而且**不会跨越多个字段误取**。地址是例外 ——
    它天然跨多行，所以走 :func:`_extract_address` 单独处理。
    """
    for position, line in enumerate(lines):
        value = _value_after_label(line, label)
        if value:
            return value
        # 本行只有标签（或标签后为空）：看下一行
        cleaned = _clean_line(line)
        if label in cleaned and position + 1 < len(lines):
            following = _clean_line(lines[position + 1])
            if following and not _looks_like_a_label(following):
                return following
    return None


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
    value = _value_for_label(lines, "姓名")
    if not value:
        return None
    value = re.split(r"性别|民族|出生|住址|公民身份号码|身份号码|身份证号", value)[0]
    return value or None


def _extract_labeled_value(lines: list[str], label: str, stop_labels: tuple[str, ...]) -> str | None:
    value = _value_for_label(lines, label)
    if not value:
        return None
    for stop_label in stop_labels:
        stop_index = value.find(stop_label)
        if stop_index >= 0:
            value = value[:stop_index]
    return value or None


def _extract_birth(ocr_text: str) -> str | None:
    """抽取出生日期。

    ``出`` 是可选的：模糊图上 ``出生`` 会被认成 ``出``
    （真实样本 ``front/blur/id_front_0002.jpg`` 就是 ``出1996年1月12日``）。
    这类「标签被截断」在 ``住址``（→ ``址``）上已经遇到过，
    同一类 OCR 退化，判据应当一致。

    标签放宽之后仍要求完整日期 —— 只认 ``出`` 不认日期会误吃
    「出生地」这类无关文本。
    """
    normalized = _clean_text(ocr_text)
    match = re.search(r"出(?:生)?((?:19|20)\d{2})年?(\d{1,2})月?(\d{1,2})日?", normalized)
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


#: 地址之后出现的标签：见到就停。
_ADDRESS_STOP_LABELS = ("公民身份号码", "身份号码", "身份证号", "签发机关", "有效期限")
#: 地址之前出现的标签：见到就说明还没到地址。
_ADDRESS_BEFORE_LABELS = ("姓名", "性别", "民族", "出生")

#: 地址块里要丢掉的噪声行。
#:
#: 真实 OCR 会在证件底纹上认出一串没有意义的短串（合成集里是
#: ``Tip bng`` / ``Chis`` 这类），它们夹在地址中间。判据是「短且不含中文」——
#: 地址行必然含中文，而这些噪声是纯拉丁字母。
_ADDRESS_NOISE = re.compile(r"^[A-Za-z0-9\s.,\-]{0,12}$")


def _extract_address(lines: list[str]) -> str | None:
    """抽取住址。地址**天然跨多行**，是唯一需要连续收集的字段。

    三种真实形状都要支持：

    1. ``住址 东湖省丹江市城东区样本街284号`` —— 标签与值同行（mock 形状）
    2. ``住址`` + ``东湖省丹江市城东区样本`` + ``街284号`` —— 标签单独一行，
       值分多行（**真实 OCR 的主要形状**）
    3. ``址东湖省丹江市城东区样本`` —— 标签被 OCR 截断（模糊图上 ``住`` 丢了），
       值仍在本行

    所以匹配标签时放宽到「以住址的任一字符开头且行内含地址特征」是不必要的 ——
    技巧在于**先找标签行，再决定是不是把这一行本身也算作值**。
    """
    parts: list[str] = []
    collecting = False

    for line in lines:
        if not collecting:
            value = _value_after_label(line, "住址")
            if value:
                parts.append(value)
                collecting = True
                continue
            # 标签被截断：行以「址」开头（模糊图上 住 丢了）
            cleaned = _clean_line(line)
            if cleaned.startswith("址"):
                parts.append(cleaned[1:])
                collecting = True
                continue
            if "住址" in cleaned:
                collecting = True
            continue

        if any(label in line for label in _ADDRESS_STOP_LABELS):
            break
        if any(label in line for label in _ADDRESS_BEFORE_LABELS):
            break
        if _ADDRESS_NOISE.match(line):
            continue  # 底纹噪声，跳过而不是中断 —— 地址还在后面
        if line:
            parts.append(line)

    address = "".join(part for part in parts if part)
    return address or None


def _normalize_date(value: str) -> str:
    match = DATE_PATTERN.search(value)
    if not match:
        return value
    year, month, day = match.groups()
    return f"{year}.{int(month):02d}.{int(day):02d}"


def _extract_valid_period(ocr_text: str) -> str | None:
    """抽取有效期限。

    先在**整段文本**里找日期区间 —— 这样即使标签与值分行、
    甚至跨越 ``\\n`` 也能匹配上（``_clean_text`` 会把换行去掉）。
    找不到再退回按行取标签后的值。
    """
    normalized = _clean_text(ocr_text)
    match = VALID_PERIOD_PATTERN.search(normalized)
    if match:
        start, end = match.groups()
        return f"{_normalize_date(start)}-{_normalize_date(end)}"

    lines = [_clean_line(line) for line in ocr_text.splitlines() if _clean_line(line)]
    value = _value_for_label(lines, "有效期限")
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

