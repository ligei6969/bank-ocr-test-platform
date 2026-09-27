"""身份证解析在**真实 OCR 输出形状**下的测试。

为什么单开一个文件
------------------
``test_id_card_parser.py`` 里的用例用的是 mock 的输出形状
（``姓名 李雷``、``性别 男 民族 苗`` —— 标签与值同行）。那个形状是**设计出来的**，
不是观察到的：真实 PaddleOCR 把标签和值检测成**两个独立的文本框**。

于是实现与测试共享同一个错误假设，一起错，测不出差异。CTE-2 录制真实
OCR 观测后，身份证字段解析在真实形状下 0/10 —— 而全部测试是绿的。

本文件用**快照里真实录到的文本行**做输入，补上这一课。
每条用例的输入都注明来源样本，可回快照核对。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.id_card_parser import (
    detect_id_card_side,
    parse_id_card_back_fields,
    parse_id_card_front_fields,
)

SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "annotations" / "ocr_outputs.json"
)


def _observed_texts(image_path: str) -> list[str]:
    """从快照取真实录到的文本行；快照不存在则跳过。"""
    if not SNAPSHOT_PATH.is_file():
        pytest.skip("没有 OCR 快照；先跑 scripts/record_ocr_snapshot")
    payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    observation = (payload.get("observations") or {}).get(image_path)
    if observation is None:
        pytest.skip(f"{image_path} 不在快照里")
    return list(observation["ocr_texts"])


# ── 跨行取值：标签与值分成两个文本框 ──────────────────────────────────────────

def test_label_and_value_on_separate_lines() -> None:
    """真实 OCR 最常见的形状：``姓名`` 一行、``沈梓欣`` 另一行。

    这是 CTE-2 暴露的核心缺陷 —— ``_value_after_label`` 原来只看本行。
    """
    text = "\n".join(
        [
            "姓名",
            "沈梓欣",
            "性别女",
            "民族回",
            "出生1990年8月31日",
            "住址",
            "东湖省丹江市城东区样本",
            "街284号",
            "公民身份号码",
            "00000019900831001X",
        ]
    )

    parsed = parse_id_card_front_fields(text)

    assert parsed["name"] == "沈梓欣"
    assert parsed["gender"] == "女"
    assert parsed["nation"] == "回"
    assert parsed["address"] == "东湖省丹江市城东区样本街284号"
    assert parsed["id_number"] == "00000019900831001X"


def test_same_line_shape_still_works() -> None:
    """向后兼容：mock 那种「标签 值」同行的形状必须继续解析正确。

    改动不能只顾真实形状而把 mock 形状弄坏 —— 平台侧的单测、前端演示
    仍然走 mock。
    """
    text = "\n".join(
        [
            "姓名 李雷",
            "性别 男 民族 苗",
            "出生 1986年1月22日",
            "住址 安徽省月江市城东区文昌街64号",
            "公民身份号码 110101198601220011",
        ]
    )

    parsed = parse_id_card_front_fields(text)

    assert parsed["name"] == "李雷"
    assert parsed["gender"] == "男"
    assert parsed["nation"] == "苗"
    assert parsed["address"] == "安徽省月江市城东区文昌街64号"
    assert parsed["id_number"] == "110101198601220011"


def test_cross_line_lookup_does_not_swallow_the_next_field() -> None:
    """跨行取值不能把**下一个字段**的值当成自己的值。

    ``出生`` 后面跟的是 ``住址`` 时，name/gender 不该越界吃到地址。
    """
    text = "\n".join(
        [
            "姓名",
            "张怡",
            "性别",
            "男",
            "民族",
            "回",
            "出生1996年1月12日",
            "住址",
            "安北省明港市城东区样本",
            "街57号",
        ]
    )

    parsed = parse_id_card_front_fields(text)

    assert parsed["name"] == "张怡"
    assert parsed["gender"] == "男"
    assert parsed["nation"] == "回"
    assert parsed["address"] == "安北省明港市城东区样本街57号"


# ── 地址：多行拼接 + 噪声行 ───────────────────────────────────────────────────

def test_address_skips_ocr_noise_between_lines() -> None:
    """证件底纹上会认出无意义的短串（``Tip bng`` / ``Chis``），地址要跨过它们。

    判据是「短且不含中文」—— 地址行必然含中文。
    """
    text = "\n".join(
        [
            "姓名",
            "沈梓欣",
            "住址",
            "东湖省丹江市城东区样本",
            "Tip bng",
            "街284号",
            "Chis",
            "公民身份号码",
            "非真实证件",
        ]
    )

    parsed = parse_id_card_front_fields(text)

    assert parsed["address"] == "东湖省丹江市城东区样本街284号", "噪声行应被跳过而不是中断收集"


def test_address_stops_at_the_next_label() -> None:
    text = "\n".join(
        ["住址", "安北省明港市城东区样本", "街57号", "公民身份号码", "110101199601120011"]
    )

    parsed = parse_id_card_front_fields(text)

    assert parsed["address"] == "安北省明港市城东区样本街57号"
    assert "110101" not in (parsed["address"] or "")


def test_truncated_address_label_is_tolerated() -> None:
    """模糊图上 ``住址`` 被认成 ``址`` —— 值仍应被取到。

    真实样本 ``front/blur/id_front_0001.jpg`` 就是这个形状。
    """
    text = "\n".join(["址东湖省丹江市城东区样本", "街284号", "公民身份号码"])

    parsed = parse_id_card_front_fields(text)

    assert parsed["address"] == "东湖省丹江市城东区样本街284号"


# ── 身份证号：前导零 ─────────────────────────────────────────────────────────

def test_id_number_with_leading_zeros_is_parsed() -> None:
    """真实证件号的行政区划码可能是前导零。

    原正则用 ``[1-9]`` 打头，把这些号码全判为「没认出来」——
    表现和 OCR 失败一模一样，很容易归错因。
    """
    for number in ("00000019900831001X", "00000019960112002X"):
        parsed = parse_id_card_front_fields(f"公民身份号码\n{number}")

        assert parsed["id_number"] == number, number


def test_the_old_pattern_is_the_reason_it_looked_like_an_ocr_failure() -> None:
    """留住这个对照：``[1-9]`` 打头的旧写法的确匹配不到这些号码。"""
    import re

    old = re.compile(
        r"(?<!\d)([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)"
    )

    assert old.search("00000019900831001X") is None, "旧写法匹配不到，这正是当初的缺陷"


def test_id_number_pattern_still_rejects_obvious_non_numbers() -> None:
    """放宽前导零之后不能变得什么都吃。"""
    parsed = parse_id_card_front_fields("公民身份号码 123456789012345678")

    assert parsed["id_number"] is None, "19 位数字不该被当成身份证号"


# ── 反面 ─────────────────────────────────────────────────────────────────────

def test_issue_authority_across_lines() -> None:
    """真实样本 ``back/dark/id_back_0001.jpg`` 的形状：``签发机关`` 单独一行。"""
    text = "\n".join(
        ["中华人民共和国", "居民身份证", "仅供OCR测试", "签发机关", "测试公安局城东分局"]
    )

    parsed = parse_id_card_back_fields(text)

    assert parsed["issue_authority"] == "测试公安局城东分局"


def test_valid_period_across_lines() -> None:
    text = "\n".join(
        ["中华人民共和国", "居民身份证", "签发机关", "测试公安局", "有效期限", "2014.10.27-2024.10.27"]
    )

    parsed = parse_id_card_back_fields(text)

    assert parsed["valid_period"] == "2014.10.27-2024.10.27"


def test_back_side_with_noise_characters() -> None:
    """极亮图上正面的字被逐个切开（``居``/``民``/``份``/``证``），反面仍应可解析。"""
    text = "\n".join(["中华人民共和国", "居", "民", "份", "证", "签发机关", "测试公安局"])

    assert detect_id_card_side(text) == "back"
    assert parse_id_card_back_fields(text)["issue_authority"] == "测试公安局"


# ── 用快照里真实录到的输入做回归 ─────────────────────────────────────────────

def test_real_snapshot_front_sample_parses_what_ocr_provided() -> None:
    """直接拿快照里的真实文本行跑 —— 这是本文件存在的意义。

    ``front/normal/id_front_0001.jpg``：OCR 认出了姓名与地址（分行），
    解析器必须把它们取出来。这一条在修复前是失败的。

    **``id_number`` 不在这里断言，因为它本来就不在 OCR 文本里** ——
    该样本的文本止于 ``公民身份号码``，后面跟的是水印 ``非真实证件``。
    这是 OCR 的局限而不是解析器的缺陷，混在一起断言会掩盖归因。
    """
    texts = _observed_texts("data/processed/id_card/front/normal/id_front_0001.jpg")

    parsed = parse_id_card_front_fields("\n".join(texts))

    assert parsed["name"] == "沈梓欣"
    assert parsed["address"] == "东湖省丹江市城东区样本街284号"


def test_only_samples_whose_ocr_read_the_number_expose_it() -> None:
    """把「OCR 没认出来」与「解析器没取到」分开，是这份测试最该守住的事。

    快照里只有 ``blur`` 那一桶的正面样本被 OCR 认出了身份证号；
    其余 8 张的文本里根本没有这串数字。如果哪天有人「修好了」
    id_number 的解析率，先看这个分布 —— 大概率是 OCR 变了而不是解析器变了。
    """
    import json as _json
    import re as _re

    payload = _json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    pattern = _re.compile(r"\d{6}(?:18|19|20)\d{2}\d{4}\d{3}[\dXx]")

    with_number: list[str] = []
    without: list[str] = []
    for key, observation in payload["observations"].items():
        if observation["doc_type"] != "id_card" or "/front/" not in key:
            continue
        joined = "".join(observation["ocr_texts"])
        (with_number if pattern.search(joined) else without).append(key)

    assert len(with_number) == 2, f"OCR 认出号码的样本数变了：{with_number}"
    assert len(without) == 8, f"OCR 没认出号码的样本数变了：{len(without)}"

    # 认出号码的那两张，解析器必须取到
    for key in with_number:
        observation = payload["observations"][key]
        parsed = parse_id_card_front_fields("\n".join(observation["ocr_texts"]))
        assert parsed["id_number"], f"{key} 的 OCR 文本里有号码，解析器却没取到"


def test_real_snapshot_dark_sample_parses_what_ocr_provided() -> None:
    """暗光样本：姓名与签发机关都被分行识别，值应取到。"""
    texts = _observed_texts("data/processed/id_card/front/dark/id_front_0001.jpg")

    parsed = parse_id_card_front_fields("\n".join(texts))

    assert parsed["name"] == "沈梓欣"

    back_texts = _observed_texts("data/processed/id_card/back/dark/id_back_0001.jpg")
    back = parse_id_card_back_fields("\n".join(back_texts))

    assert back["issue_authority"] == "测试公安局城东分局"
