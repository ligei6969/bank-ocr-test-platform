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
    ID_NUMBER_PATTERN,
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


def test_value_before_the_label_is_found() -> None:
    """**值在标签之前**的形状 —— 全量快照里这是多数（316/700）。

    PaddleOCR 的文本框返回顺序与版面阅读顺序不完全一致，
    姓名这种「标签一格、值一格」的排版特别容易调换：

        ['施然', '姓名', '性别男', ...]     ← 值在前
        ['姓名', '沈梓欣', '性别女', ...]   ← 值在后

    只往下看会漏掉前者近一半。CTE-5 在全量 2100 张上做错误率基线时
    才发现：``name`` 在 normal 桶只有 52/100，补上「往上看」之后 97/100。
    """
    text = "\n".join(["施然", "姓名", "性别男", "民族蒙古", "出生1988年1月18日"])

    parsed = parse_id_card_front_fields(text)

    assert parsed["name"] == "施然"


def test_looking_backwards_does_not_steal_the_previous_field() -> None:
    """往上看时，上一行若是**标签**就不能取 —— 否则会吃掉前一个字段。

    ``['民族', '回', '姓名', ...]`` 里，姓名不该取到 ``回``。
    """
    text = "\n".join(["性别", "男", "民族", "回", "姓名", "出生1988年1月18日"])

    parsed = parse_id_card_front_fields(text)

    assert parsed["name"] is None, "上一行是「民族」，不该被当成姓名"


def test_both_orderings_agree_on_the_same_person() -> None:
    """两种顺序必须给出同一个答案 —— 判据是对称的。"""
    forward = parse_id_card_front_fields(
        "\n".join(["姓名", "沈梓欣", "性别女", "民族回"])
    )
    backward = parse_id_card_front_fields(
        "\n".join(["沈梓欣", "姓名", "性别女", "民族回"])
    )

    assert forward["name"] == backward["name"] == "沈梓欣"


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


def test_truncated_birth_label_is_tolerated() -> None:
    """``出生`` 被认成 ``出`` —— 与 ``住址``→``址`` 同类退化，判据应当一致。

    真实样本 ``front/blur/id_front_0002.jpg`` 是 ``出1996年1月12日``。
    这个缺陷是 CTE-4 的归因信号**自动**指出来的：它把该样本的 birth
    判为「文本里有证据但解析器没取到」，一查果然是标签被截断。
    """
    parsed = parse_id_card_front_fields("出1996年1月12日")

    assert parsed["birth"] == "1996-01-12"


def test_the_birth_label_relaxation_still_requires_a_full_date() -> None:
    """放宽标签不等于放宽值 —— 没有完整日期的行不该被吃进来。"""
    assert parse_id_card_front_fields("出生地")["birth"] is None
    assert parse_id_card_front_fields("出生1996年")["birth"] is None


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


def test_the_parser_never_misses_a_number_that_ocr_actually_read() -> None:
    """**性质断言**：OCR 文本里有号码的样本，解析器必须一个不漏地取到。

    这是「OCR 没认出来」与「解析器没取到」的分界线，也是这份测试最该守的事。

    不写死具体条数 —— 快照会随样本量变化（CTE-2 的 50 张里只有 2 张有号码，
    CTE-5 扩到 2100 张后有 31 张）。写死条数会让测试在快照扩展时失败，
    而它想守的性质其实没变。

    如果哪天解析率突然变了，先看这个分布：大概率是 OCR 变了，不是解析器变了。
    """
    import json as _json
    import re as _re

    payload = _json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    checked = 0
    for key, observation in payload["observations"].items():
        if observation["doc_type"] != "id_card" or "/front/" not in key:
            continue
        texts = observation["ocr_texts"]
        # 用**解析器自己的正则**去逐行问「这里有没有一个合法形态的号码」。
        # 不用更宽松的「有没有一长串数字」—— 那会把 OCR 认多一位的
        # （实测有 `000000020040520081X`，19 位）也算成「读到了」，
        # 而 19 位本身就不是合法号码，解析器拒绝它是对的。
        if not any(ID_NUMBER_PATTERN.search(line) for line in texts):
            continue
        checked += 1
        parsed = parse_id_card_front_fields("\n".join(texts))
        assert parsed["id_number"], f"{key} 的 OCR 文本里有号码，解析器却没取到"

    assert checked > 0, "快照里没有任何一张含号码的正面样本 —— 断言成了空转"


def test_most_front_samples_genuinely_lack_the_number_in_their_text() -> None:
    """把事实记下来：**绝大多数**正面样本的 OCR 文本里没有身份证号。

    这不是解析器的问题，而是成像问题（号码区被水印覆盖）。
    写下这条是为了防止后来者把它当成解析缺陷去修 ——
    CTE-3 差点这么干过（`EVT-002`）。
    """
    import json as _json
    import re as _re

    payload = _json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    pattern = _re.compile(r"\d{17}[\dXx]")

    total = with_number = 0
    for key, observation in payload["observations"].items():
        if observation["doc_type"] != "id_card" or "/front/" not in key:
            continue
        total += 1
        if pattern.search("".join(observation["ocr_texts"])):
            with_number += 1

    assert total >= 100, "样本太少，这条比例断言没有意义"
    assert with_number / total < 0.2, (
        f"含号码的比例是 {with_number}/{total} —— 明显偏高，"
        "说明成像或 OCR 行为变了，值得重新检查归因"
    )


def test_real_snapshot_dark_sample_parses_what_ocr_provided() -> None:
    """暗光样本：姓名与签发机关都被分行识别，值应取到。"""
    texts = _observed_texts("data/processed/id_card/front/dark/id_front_0001.jpg")

    parsed = parse_id_card_front_fields("\n".join(texts))

    assert parsed["name"] == "沈梓欣"

    back_texts = _observed_texts("data/processed/id_card/back/dark/id_back_0001.jpg")
    back = parse_id_card_back_fields("\n".join(back_texts))

    assert back["issue_authority"] == "测试公安局城东分局"
