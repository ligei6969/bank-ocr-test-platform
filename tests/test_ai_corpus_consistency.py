"""Cross-checks between the platform's reason codes and the AI knowledge corpus.

The corpus is the single source of truth the AI assistant answers from. If the
platform starts emitting a reason code the corpus does not describe, the
assistant silently degrades to "语料未收录" — which is exactly the failure this
test prevents. Thresholds are extracted from the implementation source rather
than duplicated here, so changing a threshold without updating the corpus fails.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable

import pytest

from app import quality_check, rule_check
from app.main import _error_reason, review_id_card_with_reasons
from ai_service.corpus import (
    DEFAULT_DOCS,
    DOC_TYPE_BANK_CARD,
    DOC_TYPE_ID_CARD,
    all_reason_codes,
    reason_code_lookup,
)
from ai_service.explain import (
    INFRASTRUCTURE_CODES,
    QUALITY_CODES,
    classify_reason_code,
    split_labeled_sections,
)

ENTRY_ERROR_DETAILS = (
    "Unsupported file type. Upload a PNG or JPEG image.",
    "Uploaded file is not a readable image.",
    "Uploaded file is empty.",
    "Invalid OCR_MODE. Use 'mock' or 'paddle'.",
    "unexpected internal failure",
)


def emitted_reason_codes() -> set[str]:
    """Drive the real rule functions to collect every code they can produce."""
    codes: set[str] = set()

    for quality in (
        {"is_blur": True},
        {"brightness": "dark"},
        {"brightness": "bright"},
        {"has_glare": True},
    ):
        codes.update(quality_check.get_quality_reasons(quality))

    bank_card_cases = (
        ({}, {}),
        ({"card_number": "1", "valid_date": "01/25", "name": "x"}, {"quality_result": "pass"}),
        (
            {"card_number": "6222021234567890", "valid_date": "13/25", "name": "x"},
            {"quality_result": "pass"},
        ),
        (
            {"card_number": "6222021234567890", "valid_date": "01/25", "name": "x"},
            {"quality_result": "review", "quality_reasons": ["image_blur"]},
        ),
    )
    for fields, quality in bank_card_cases:
        _result, reasons = rule_check.review_bank_card_with_reasons(fields, quality)
        codes.update(reasons)

    for side in ("unknown", "front", "back"):
        _result, reasons = review_id_card_with_reasons(side, {}, {})
        codes.update(reasons)

    for detail in ENTRY_ERROR_DETAILS:
        codes.add(_error_reason(detail))

    # Written directly by the review middleware / validation handler in app/main.py
    codes.add("invalid_request")
    return codes


def _corpus_text(code: str) -> str:
    doc = reason_code_lookup().get(code)
    assert doc is not None, f"语料未收录原因码 {code}"
    return doc.content


def _source_number(function: Callable[..., Any], pattern: str) -> str:
    """Extract a numeric literal from an implementation function."""
    match = re.search(pattern, inspect.getsource(function))
    assert match, f"无法从 {function.__qualname__} 源码中匹配 {pattern!r}"
    return match.group(1)


# ── 覆盖率 ────────────────────────────────────────────────────────────────────

def test_platform_emits_at_least_the_expected_number_of_reason_codes() -> None:
    # 22 是当前实现的完整集合；数量变化时本测试会失败，提醒同步语料与文档
    assert len(emitted_reason_codes()) == 22


def test_every_emitted_reason_code_is_described_in_the_corpus() -> None:
    missing = sorted(emitted_reason_codes() - set(all_reason_codes()))

    assert missing == [], f"以下原因码缺少语料释义: {missing}"


def test_corpus_contains_no_unused_reason_codes() -> None:
    """语料反向校验：收录了平台根本不会产出的码，说明语料已过期。"""
    extra = sorted(set(all_reason_codes()) - emitted_reason_codes())

    assert extra == [], f"语料中存在平台不会产出的原因码: {extra}"


def test_reason_code_lookup_is_unambiguous() -> None:
    seen: dict[str, str] = {}
    duplicates: list[str] = []

    for doc in DEFAULT_DOCS:
        for code in doc.reason_codes:
            if code in seen:
                duplicates.append(f"{code}（{seen[code]} 与 {doc.doc_id}）")
            else:
                seen[code] = doc.doc_id

    assert duplicates == [], f"同一原因码被多条文档重复收录: {duplicates}"


def test_quality_and_infrastructure_partitions_cover_the_corpus() -> None:
    known = set(all_reason_codes())

    assert QUALITY_CODES <= known
    assert INFRASTRUCTURE_CODES <= known
    # 三类归属必须互斥，否则前端标签会打架
    assert QUALITY_CODES.isdisjoint(INFRASTRUCTURE_CODES)
    for code in known:
        assert classify_reason_code(code) in {"quality", "field", "infrastructure"}


def test_every_reason_code_document_is_searchable() -> None:
    """释义文档必须能被标签切分器解析出结构化字段，否则前端会显示空白。"""
    for code in all_reason_codes():
        doc = reason_code_lookup()[code]
        lead, sections = split_labeled_sections(doc.content)

        assert lead, f"{code} 缺少摘要句"
        assert "业务含义" in sections or lead, f"{code} 缺少业务含义"
        assert "处置建议" in sections, f"{code} 缺少处置建议"
        # 标题必须出现原因码字面量，否则检索与列表展示都会吃亏
        # （部分文档按面别合并了多个原因码，所以用包含而不是前缀判断）
        assert code in doc.title, f"{doc.title} 未包含原因码 {code}"


# ── 阈值一致性 ────────────────────────────────────────────────────────────────

def test_blur_threshold_matches_the_corpus() -> None:
    threshold = _source_number(quality_check.detect_blur, r"variance\s*<\s*([0-9.]+)")

    assert threshold in _corpus_text("image_blur")


def test_dark_brightness_threshold_matches_the_corpus() -> None:
    threshold = _source_number(quality_check.detect_brightness, r"mean_value\s*<\s*([0-9.]+)")

    assert threshold in _corpus_text("image_dark")


def test_bright_brightness_threshold_matches_the_corpus() -> None:
    threshold = _source_number(quality_check.detect_brightness, r"mean_value\s*>\s*([0-9.]+)")

    assert threshold in _corpus_text("image_bright")


def test_glare_thresholds_match_the_corpus() -> None:
    content = _corpus_text("glare_detected")

    assert str(quality_check.GLARE_VALUE_THRESHOLD) in content
    assert str(quality_check.GLARE_SATURATION_THRESHOLD) in content
    # 实现里是 0.005 的比例，语料用百分比表述
    ratio_percent = f"{quality_check.GLARE_COMPONENT_RATIO_THRESHOLD * 100:g}%"
    assert ratio_percent in content


def test_card_number_length_rule_matches_the_corpus() -> None:
    source = inspect.getsource(rule_check.is_valid_card_number)
    match = re.search(r"\\d\{(\d+),(\d+)\}", source)
    assert match, "无法从 is_valid_card_number 提取长度区间"

    content = _corpus_text("invalid_card_number")
    assert match.group(1) in content
    assert match.group(2) in content


def test_expiry_month_range_matches_the_corpus() -> None:
    source = inspect.getsource(rule_check.is_valid_expiry)
    assert "0[1-9]" in source and "1[0-2]" in source

    content = _corpus_text("invalid_valid_date")
    assert "01" in content and "12" in content


def test_required_bank_card_fields_are_all_described() -> None:
    for field in rule_check.REQUIRED_BANK_CARD_FIELDS:
        code = f"missing_{field}"
        assert code in all_reason_codes(), f"必填字段 {field} 缺少 missing_ 语料"


# ── 语料元信息 ────────────────────────────────────────────────────────────────

def test_doc_type_scoping_rules_out_wrong_document_families() -> None:
    """银行卡专属原因码不应被标记为身份证可见，反之亦然。"""
    lookup = reason_code_lookup()

    assert DOC_TYPE_BANK_CARD in lookup["missing_card_number"].doc_types
    assert DOC_TYPE_ID_CARD not in lookup["missing_card_number"].doc_types
    assert DOC_TYPE_ID_CARD in lookup["unknown_id_card_side"].doc_types
    assert DOC_TYPE_BANK_CARD not in lookup["unknown_id_card_side"].doc_types


def test_capture_guide_covers_the_three_user_facing_rules() -> None:
    """语料必须覆盖用户端 user_home.html 展示的三条拍摄规范。"""
    guide_content = "".join(
        doc.content for doc in DEFAULT_DOCS if doc.category == "capture_guide"
    )

    for keyword in ("边缘完整", "文字清晰", "光线均匀"):
        assert keyword in guide_content, f"拍摄规范缺少「{keyword}」"


def test_corpus_documents_have_unique_ids() -> None:
    ids = [doc.doc_id for doc in DEFAULT_DOCS]

    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("code", sorted(all_reason_codes()))
def test_reason_code_document_mentions_its_own_code(code: str) -> None:
    """释义正文必须出现原因码字面量，否则词法检索会漏召回。"""
    assert code in _corpus_text(code)


# ── 阈值机器可读副本 vs 平台实现 ──────────────────────────────────────────────
#
# ``ai_service/thresholds.py`` 是阈值的机器可读副本，Agent 的 recompute_quality
# 工具依赖它反推原因码。阈值一旦被抄两遍就一定会漂移，所以这里把平台源码里的
# 真值读出来逐项比对 —— 靠自觉不如靠测试。
#
# 注意方向：本测试在**平台侧**运行，因此可以 import app.quality_check；
# ai_service 本身刻意不依赖平台的 OpenCV 实现，保持轻依赖。

from ai_service.thresholds import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    METRIC_BLUR_VARIANCE,
    METRIC_BRIGHTNESS_MEAN,
    METRIC_GLARE_COMPONENT_RATIO,
    QUALITY_REASON_CODES,
    derive_quality_reasons,
)


def test_ai_blur_threshold_matches_platform_source() -> None:
    threshold = _source_number(quality_check.detect_blur, r"variance\s*<\s*([0-9.]+)")

    assert float(threshold) == DEFAULT_THRESHOLDS.blur_variance


def test_ai_dark_brightness_threshold_matches_platform_source() -> None:
    threshold = _source_number(quality_check.detect_brightness, r"mean_value\s*<\s*([0-9.]+)")

    assert float(threshold) == DEFAULT_THRESHOLDS.brightness_dark


def test_ai_bright_brightness_threshold_matches_platform_source() -> None:
    threshold = _source_number(quality_check.detect_brightness, r"mean_value\s*>\s*([0-9.]+)")

    assert float(threshold) == DEFAULT_THRESHOLDS.brightness_bright


def test_ai_glare_thresholds_match_platform_constants() -> None:
    assert DEFAULT_THRESHOLDS.glare_value == quality_check.GLARE_VALUE_THRESHOLD
    assert DEFAULT_THRESHOLDS.glare_saturation == quality_check.GLARE_SATURATION_THRESHOLD
    assert DEFAULT_THRESHOLDS.glare_component_ratio == pytest.approx(
        quality_check.GLARE_COMPONENT_RATIO_THRESHOLD
    )


def test_ai_quality_reason_codes_cover_every_platform_emitted_code() -> None:
    """平台每种质检判定都会出的原因码，AI 侧必须都声明。"""
    samples = [
        {"is_blur": True},
        {"brightness": "dark"},
        {"brightness": "bright"},
        {"has_glare": True},
    ]
    emitted: set[str] = set()
    for sample in samples:
        emitted.update(quality_check.get_quality_reasons(sample))

    assert emitted == set(QUALITY_REASON_CODES)


@pytest.mark.parametrize(
    ("metrics", "platform_flags"),
    [
        (
            {METRIC_BLUR_VARIANCE: 10.0, METRIC_BRIGHTNESS_MEAN: 128.0},
            {"is_blur": True, "brightness": "normal", "has_glare": False},
        ),
        (
            {METRIC_BLUR_VARIANCE: 500.0, METRIC_BRIGHTNESS_MEAN: 30.0},
            {"is_blur": False, "brightness": "dark", "has_glare": False},
        ),
        (
            {METRIC_BLUR_VARIANCE: 500.0, METRIC_BRIGHTNESS_MEAN: 250.0},
            {"is_blur": False, "brightness": "bright", "has_glare": False},
        ),
        (
            {
                METRIC_BLUR_VARIANCE: 500.0,
                METRIC_BRIGHTNESS_MEAN: 128.0,
                METRIC_GLARE_COMPONENT_RATIO: 0.05,
            },
            {"is_blur": False, "brightness": "normal", "has_glare": True},
        ),
        (
            {
                METRIC_BLUR_VARIANCE: 500.0,
                METRIC_BRIGHTNESS_MEAN: 128.0,
                METRIC_GLARE_COMPONENT_RATIO: 0.0,
            },
            {"is_blur": False, "brightness": "normal", "has_glare": False},
        ),
    ],
)
def test_ai_recompute_agrees_with_platform_judgement(
    metrics: dict,
    platform_flags: dict,
) -> None:
    """同一组原始指标，AI 侧重推的原因码必须与平台判定逐项一致。

    这是 recompute_quality 之所以可信的全部依据：两边跑的是同一套阈值语义。
    """
    expected = quality_check.get_quality_reasons(platform_flags)

    assert derive_quality_reasons(metrics) == expected


def test_ai_blur_boundary_is_exclusive_like_the_platform() -> None:
    """平台用的是 ``variance < 80.0``，等于阈值不算模糊 —— 副本必须是同一语义。"""
    at_threshold = derive_quality_reasons(
        {METRIC_BLUR_VARIANCE: DEFAULT_THRESHOLDS.blur_variance, METRIC_BRIGHTNESS_MEAN: 128.0}
    )
    just_below = derive_quality_reasons(
        {
            METRIC_BLUR_VARIANCE: DEFAULT_THRESHOLDS.blur_variance - 0.1,
            METRIC_BRIGHTNESS_MEAN: 128.0,
        }
    )

    assert at_threshold == []
    assert just_below == ["image_blur"]
