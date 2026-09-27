"""判断「OCR 文本里到底有没有这个字段的证据」。

为什么需要这个模块
------------------
``missing_card_number`` / ``missing_id_number`` 这类原因码把两种
**完全不同的故障**塌缩成了一个信号：

1. **OCR 没认出来** —— 文本里根本没有这串数字/这行字，解析器无从下手；
2. **解析器没取到** —— 文本里有，解析规则没匹配上。

对下游（审核员、指标、改进方向）这两者的含义相反：
第 1 种要动图像侧（采集质量、模型），第 2 种要动解析代码。

CTE-3 踩过这个坑：``EVT-002`` 最初把 ``id_number 0/10`` 整个归因给解析器，
修完解析器数字没变 —— 逐条核对才发现 8/10 的样本里 OCR 压根没产出那串数字。
**如果当时有这个信号，第一步就不会归错因。**

判据怎么定
----------
按字段类型给一个「证据正则」：身份证号是 18 位数字（含 X 校验位），
有效期是日期区间，银行卡号是 16–19 位数字。命中即为「文本里有证据」。

这个判据**刻意比解析器的匹配条件宽**：它的职责是回答「值在不在文本里」，
不是「值合不合法」。一个格式非法但确实存在于文本中的卡号，
应当被判为「有证据」（解析器该负责），而不是「OCR 没认出来」。
两者混淆会再次归错因。
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, Mapping, Pattern, Sequence


def _digits(text: str) -> str:
    """去掉空白与常见分隔符，便于对「带空格的卡号」做匹配。

    单独用 ``_clean`` 而不是直接搜原文：真实 OCR 会把卡号认成
    ``6222 0202 0202 0001`` 这种带空格的形式，而证据判据关心的是数字本身。
    """
    return re.sub(r"[\s\-]", "", text or "")


#: 字段 → 证据正则。正则都作用在**去空白**后的文本上。
#:
#: **一律比解析器的条件宽**（见模块 docstring）：这里回答「值在不在文本里」，
#: 不是「值合不合法」。所以：
#:
#: * 卡号：16–19 位数字，**不校验 Luhn**；
#: * 身份证号：18 位，**不校验校验位、不要求首位非零**（前导零是真实的）；
#: * 有效期：任意两位数字 + ``/`` + 两位数字，**不校验月份范围** ——
#:   ``13/45`` 这种对解析器非法的值，在文本里依然是「有证据」；
#: * 日期：四位年 + 月 + 日，**不校验月日范围**；
#: * 姓名/住址/机关等文本字段：无法用正则判定，见 :data:`TEXT_FIELD_ASSUMPTION`。
_EVIDENCE_PATTERNS: Dict[str, Pattern[str]] = {
    "card_number": re.compile(r"\d{16,19}"),
    "id_number": re.compile(r"\d{17}[\dXx]"),
    "valid_date": re.compile(r"\d{2}/\d{2}"),
    "valid_period": re.compile(r"\d{4}[.\-/年]\d{1,2}[.\-/月]\d{1,2}日?"),
    "birth": re.compile(r"\d{4}[.\-/年]\d{1,2}[.\-/月]\d{1,2}日?"),
}

#: 无法用正则判定的文本字段。
#:
#: 姓名、住址、签发机关这类字段没有可靠的字面模式 —— 「沈梓欣」和
#: 「测试公安局城东分局」在文本里长什么样，取决于 OCR 认出了什么。
#: 硬造一个正则只会给出假的确定感。
#:
#: 所以对这些字段**假定文本里有证据**（即缺失归因给解析器）。
#: 这个默认方向是刻意选的：
#:
#: * 归因给解析器 → 有人去查解析代码，发现其实该改图像侧，代价是一次排查；
#: * 归因给 OCR → 有人去调采集，而问题其实在解析，代价是**改错地方**。
#:
#: 两者不对称，所以默认取「更可能被复查」的那一侧。它们的缺失本来就少见，
#: 而 ``EVT-002`` 证明的恰恰是「解析器确实会漏取文本里明明有的值」。
TEXT_FIELD_ASSUMPTION = True

#: 银行卡必填字段（与 ``rule_check.REQUIRED_BANK_CARD_FIELDS`` 对齐）。
BANK_CARD_EVIDENCE_FIELDS: Sequence[str] = ("card_number", "valid_date", "name")
#: 身份证正面必填字段。
ID_CARD_FRONT_EVIDENCE_FIELDS: Sequence[str] = (
    "name",
    "gender",
    "nation",
    "birth",
    "address",
    "id_number",
)
#: 身份证反面必填字段。
ID_CARD_BACK_EVIDENCE_FIELDS: Sequence[str] = ("issue_authority", "valid_period")


def has_evidence(field: str, ocr_text: str) -> bool:
    """OCR 文本里有没有 ``field`` 的证据。

    未知字段返回 :data:`TEXT_FIELD_ASSUMPTION`（见其说明）。
    """
    pattern = _EVIDENCE_PATTERNS.get(field)
    if pattern is None:
        return TEXT_FIELD_ASSUMPTION
    return bool(pattern.search(_digits(ocr_text)))


def evidence_missing_fields(
    fields: Mapping[str, object],
    ocr_text: str,
    *,
    required: Iterable[str],
) -> list[str]:
    """返回哪些必填字段**既没解析出来、文本里也没有证据**。

    只有同时满足两件事才算「OCR 没认出来」：

    * 解析结果里这个字段是空的；
    * 文本里找不到这个字段的证据。

    「解析为空但文本里有证据」的字段**不在这里** —— 那是解析器的责任，
    调用方应当照常给出 ``missing_<field>`` 而不附加归因码。
    """
    missing: list[str] = []
    for field in required:
        if fields.get(field):
            continue
        if not has_evidence(field, ocr_text):
            missing.append(field)
    return missing


def attribution_reasons(
    fields: Mapping[str, object],
    ocr_text: str,
    *,
    required: Iterable[str],
    prefix: str = "evidence_missing_",
) -> list[str]:
    """把归因结果转成原因码，供规则层直接追加。

    命名取 ``evidence_missing_<field>`` 而不是 ``ocr_failed_<field>``：
    这个信号说的是「**证据**不在文本里」，是一个关于输入的**观察**；
    说成「OCR 失败」是对原因的**断言**，而同一种观察也可能来自
    图像本身就没有那串数字（印得模糊、被遮挡）。观察可以确定，
    原因不行 —— 命名不该超出证据。
    """
    return [f"{prefix}{field}" for field in evidence_missing_fields(
        fields, ocr_text, required=required
    )]


__all__ = (
    "BANK_CARD_EVIDENCE_FIELDS",
    "ID_CARD_BACK_EVIDENCE_FIELDS",
    "ID_CARD_FRONT_EVIDENCE_FIELDS",
    "TEXT_FIELD_ASSUMPTION",
    "attribution_reasons",
    "evidence_missing_fields",
    "has_evidence",
)
