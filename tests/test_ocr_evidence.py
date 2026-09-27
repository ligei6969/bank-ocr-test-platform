"""归因信号的测试：「OCR 没认出来」vs「解析器没取到」。

这个模块存在的唯一理由是让这两种故障**在原因码上可分**。
CTE-3 踩过归错的代价：``EVT-002`` 把 ``id_number 0/10`` 整个算作解析器的
责任，修完解析器数字没变 —— 8/10 的样本里 OCR 压根没产出那串数字。
如果当时有这个信号，第一步就不会归错因。

所以这里的测试重点不是「正则匹配得对不对」，而是**归因方向对不对**：
一个有证据的缺失必须能被认出来，一个没证据的缺失不能被误判成解析问题。
"""

from __future__ import annotations

import pytest

from app.ocr_evidence import (
    BANK_CARD_EVIDENCE_FIELDS,
    ID_CARD_BACK_EVIDENCE_FIELDS,
    ID_CARD_FRONT_EVIDENCE_FIELDS,
    TEXT_FIELD_ASSUMPTION,
    attribution_reasons,
    evidence_missing_fields,
    has_evidence,
)


# ── 有证据 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "field,text",
    [
        ("card_number", "TEST BANK 6222 0202 0202 0001"),
        ("card_number", "6222020202020001"),  # 无空格
        ("card_number", "卡号6222020202020001"),  # 与其他字相邻
        ("id_number", "公民身份号码 00000019900831001X"),
        ("id_number", "110101198601220011"),
        ("valid_date", "VALID THRU 12/30"),
        ("valid_period", "有效期限 2020.01.01-2040.01.01"),
        ("birth", "出生 1986年1月22日"),
    ],
)
def test_evidence_is_found(field: str, text: str) -> None:
    assert has_evidence(field, text), f"{field} 的证据没有被认出来：{text!r}"


def test_a_number_split_by_whitespace_still_counts_as_evidence() -> None:
    """真实 OCR 会把卡号认成 ``6222 0202 0202 0001``。

    证据判据去空白后再匹配 —— 否则最该被判为「有证据」的形态反而漏掉。
    """
    assert has_evidence("card_number", "6222 0202 0202 0001")


# ── 没证据 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "field,text",
    [
        ("card_number", ""),
        ("card_number", "TEST BANK"),
        ("card_number", "1234"),  # 位数不够
        ("id_number", "公民身份号码"),  # 只有标签，号码没认出来
        ("id_number", "公民身份号码 非真实证件"),
        ("valid_date", "VALID THRU"),
        ("valid_period", "有效期限"),
        ("birth", "出生"),
    ],
)
def test_absence_of_evidence_is_detected(field: str, text: str) -> None:
    assert not has_evidence(field, text), f"{field} 不该被判为有证据：{text!r}"


def test_a_longer_digit_run_still_contains_evidence() -> None:
    """20 位数字里含 16–19 位的子串，判为「有证据」是**正确**的。

    这个模块只回答「文本里有没有一截像卡号的数字」。要判「这串数字
    够不够格当卡号」是解析器与规则层的事（``is_valid_card_number``）。
    在这里加长度上界会把解析器的责任又推给采集 —— 正是要避免的。
    """
    assert has_evidence("card_number", "12345678901234567890")


# ── 判据刻意宽于解析器 ────────────────────────────────────────────────────────

def test_an_illegal_but_present_value_counts_as_evidence() -> None:
    """格式非法但**确实存在于文本里**的值，应判为「有证据」。

    本模块回答的是「值在不在文本里」，不是「值合不合法」。
    把不合法也判成「OCR 没认出来」，就会把解析器的责任推给采集。
    """
    # Luhn 不通过的 17 位数字：解析器会判 invalid_card_number，但它在文本里
    assert has_evidence("card_number", "62220202020200019")
    # 月份 13 不合法，但文本里确实有这个形态
    assert has_evidence("valid_date", "13/45")


def test_a_leading_zero_id_number_counts_as_evidence() -> None:
    """前导零是真实存在的证件号形态（合成集里就是 000000...）。"""
    assert has_evidence("id_number", "00000019900831001X")


# ── 文本字段：默认归因给解析器 ────────────────────────────────────────────────

@pytest.mark.parametrize("field", ["name", "address", "issue_authority", "gender"])
def test_text_fields_default_to_parser_responsibility(field: str) -> None:
    """姓名/住址/机关没有可靠的字面模式，一律按解析器责任处理。

    方向是刻意选的（见 ``TEXT_FIELD_ASSUMPTION``）：归错给解析器的代价是
    一次排查，归错给采集的代价是**改错地方**。两者不对称。
    """
    assert has_evidence(field, "") is TEXT_FIELD_ASSUMPTION


def test_text_fields_never_produce_attribution_codes() -> None:
    """所以姓名缺失不会附带归因码 —— 只有 missing_name。"""
    reasons = attribution_reasons(
        {"name": None}, "", required=BANK_CARD_EVIDENCE_FIELDS
    )

    assert "evidence_missing_name" not in reasons
    # 但卡号与有效期确实没有证据 → 有归因码
    assert "evidence_missing_card_number" in reasons
    assert "evidence_missing_valid_date" in reasons


# ── 归因方向：这个模块存在的意义 ──────────────────────────────────────────────

def test_a_missing_field_with_evidence_is_the_parsers_fault() -> None:
    """**核心用例**：文本里有卡号，但解析结果为空 → 不是 OCR 的问题。

    这种情况**不该**产出归因码。EVT-002 的教训：把它算成 OCR 就会误导改进方向。
    """
    fields = {"card_number": None}
    text = "TEST BANK 6222 0202 0202 0001"
    reasons = attribution_reasons(fields, text, required=("card_number",))

    assert reasons == [], "文本里有证据，缺失该归解析器"


def test_a_missing_field_without_evidence_is_ocr_limited() -> None:
    """**核心用例**：文本里根本没有那串数字 → OCR 的限制。

    对应 CTE-2 快照里的 8/10 —— 修解析器不会让这个数字变好。
    """
    fields = {"card_number": None}
    text = "TEST BANK"
    reasons = attribution_reasons(fields, text, required=("card_number",))

    assert reasons == ["evidence_missing_card_number"]


def test_a_present_field_never_gets_an_attribution_code() -> None:
    fields = {"card_number": "6222020202020001"}

    assert attribution_reasons(fields, "", required=("card_number",)) == []


def test_evidence_missing_fields_only_reports_the_unexplained_ones() -> None:
    fields = {"card_number": None, "valid_date": None, "name": None}
    text = "6222 0202 0202 0001"  # 只有卡号有证据

    missing = evidence_missing_fields(fields, text, required=BANK_CARD_EVIDENCE_FIELDS)

    assert "card_number" not in missing, "卡号有证据，不算 OCR 限制"
    assert "valid_date" in missing
    assert "name" not in missing, "文本字段默认不归因给 OCR"


# ── 字段清单与规则层对齐 ──────────────────────────────────────────────────────

def test_evidence_field_lists_match_the_required_fields() -> None:
    """证据字段清单必须与规则层的必填字段一致 —— 少一个就不会被检查。"""
    from app.main import review_id_card_with_reasons
    from app.rule_check import REQUIRED_BANK_CARD_FIELDS

    assert tuple(REQUIRED_BANK_CARD_FIELDS) == BANK_CARD_EVIDENCE_FIELDS
    # 身份证按面别：正面 6 项、反面 2 项，与 main.py 里的 required 一致
    assert len(ID_CARD_FRONT_EVIDENCE_FIELDS) == 6
    assert len(ID_CARD_BACK_EVIDENCE_FIELDS) == 2
    assert callable(review_id_card_with_reasons)
