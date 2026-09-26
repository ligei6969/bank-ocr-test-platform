"""Tests for boundary-case detection (P2.3 dual-judge input)."""

from __future__ import annotations

import pytest

from app import boundary
from app.boundary import (
    FALSE_POSITIVE_CODE,
    NEAR_BLUR_THRESHOLD,
    NEAR_BRIGHT_THRESHOLD,
    NEAR_DARK_THRESHOLD,
    NEAR_GLARE_THRESHOLD,
    NEAR_MISS_FIELD,
    detect_boundary_case,
)


def _quality(**metrics) -> dict:
    return {"quality_metrics": metrics}


# ── 权限/成本边界：只有 review 才可能进双判 ──────────────────────────────────

@pytest.mark.parametrize("result", ["pass", "reject", "error", ""])
def test_non_review_results_are_never_boundary(result: str) -> None:
    """pass / reject / error 一律不进双判 —— 尤其 reject 不该有被复核的机会。"""
    criteria = detect_boundary_case(
        {"card_number": "6222020202020001"},
        _quality(blur_laplacian_variance=75.0),
        result,
        ["image_blur"],
    )

    assert criteria == []


def test_review_without_any_reason_code_is_not_boundary() -> None:
    """review 却无原因码 = 判定来路不明，该查规则而不是问 LLM 要不要放行。"""
    criteria = detect_boundary_case({}, _quality(), "review", [])

    assert criteria == []


# ── C1：阈值边缘 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("metric", "value", "expected"),
    [
        # 模糊阈值 80，带宽 10 → [70, 80)
        ("blur_laplacian_variance", 70.0, NEAR_BLUR_THRESHOLD),
        ("blur_laplacian_variance", 79.9, NEAR_BLUR_THRESHOLD),
        ("blur_laplacian_variance", 80.0, None),  # 恰好等于阈值不算（平台用 <）
        ("blur_laplacian_variance", 69.9, None),
        ("blur_laplacian_variance", 900.0, None),
    ],
)
def test_c1_blur_band(metric: str, value: float, expected: str | None) -> None:
    criteria = detect_boundary_case({}, _quality(**{metric: value}), "review", ["image_blur"])

    assert criterion_in(criteria, expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (55.0, NEAR_DARK_THRESHOLD),  # 暗阈值 65，带宽 10 → [55, 65)
        (64.9, NEAR_DARK_THRESHOLD),
        (65.0, None),
        (54.9, None),
    ],
)
def test_c1_dark_band(value: float, expected: str | None) -> None:
    criteria = detect_boundary_case({}, _quality(brightness_mean=value), "review", ["image_dark"])

    assert criterion_in(criteria, expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (211.0, NEAR_BRIGHT_THRESHOLD),  # 亮阈值 210，带宽 5 → (210, 215]
        (215.0, NEAR_BRIGHT_THRESHOLD),
        (210.0, None),
        (215.1, None),
    ],
)
def test_c1_bright_band(value: float, expected: str | None) -> None:
    criteria = detect_boundary_case({}, _quality(brightness_mean=value), "review", ["image_bright"])

    assert criterion_in(criteria, expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0051, NEAR_GLARE_THRESHOLD),  # 反光阈值 0.005，带宽 0.001 → (0.005, 0.006]
        (0.0060, NEAR_GLARE_THRESHOLD),
        (0.0050, None),
        (0.0061, None),
    ],
)
def test_c1_glare_band(value: float, expected: str | None) -> None:
    criteria = detect_boundary_case({}, _quality(glare_component_ratio=value), "review", ["glare_detected"])

    assert criterion_in(criteria, expected)


def test_c1_missing_or_malformed_metrics_do_not_crash() -> None:
    """老记录没有 quality_metrics（默认 {}），判据要安全跳过而不是炸。"""
    for quality in (None, {}, {"quality_metrics": None}, {"quality_metrics": {"brightness_mean": "x"}}):
        assert detect_boundary_case({}, quality, "review", ["image_blur"]) == []


def test_c1_ignores_booleans() -> None:
    """True/False 是 int 的子类，不该被当成数值指标。"""
    criteria = detect_boundary_case({}, _quality(blur_laplacian_variance=True), "review", ["image_blur"])

    assert criteria == []


# ── C2：单一可误报原因码 ────────────────────────────────────────────────────

def test_c2_single_false_positive_code_is_boundary() -> None:
    criteria = detect_boundary_case({}, _quality(), "review", ["glare_detected"])

    assert criteria == [FALSE_POSITIVE_CODE]


def test_c2_multiple_reason_codes_are_not_boundary() -> None:
    """多个原因码同时成立时有交叉证据，误报概率更低，不该占用复核预算。"""
    criteria = detect_boundary_case(
        {}, _quality(), "review", ["glare_detected", "image_blur"]
    )

    assert criteria == []


def test_c2_non_false_positive_code_is_not_boundary() -> None:
    """模糊是经过阈值标定的真实信号，不属于「可能误报」。"""
    criteria = detect_boundary_case({}, _quality(), "review", ["image_blur"])

    assert criteria == []


def test_c2_duplicate_codes_count_as_single() -> None:
    criteria = detect_boundary_case({}, _quality(), "review", ["glare_detected", "glare_detected"])

    assert criteria == [FALSE_POSITIVE_CODE]


# ── C3：字段形态接近正确 ────────────────────────────────────────────────────

@pytest.mark.parametrize("digits", ["622202020202000", "62220202020200010012"])
def test_c3_card_number_off_by_one_digit(digits: str) -> None:
    """15 或 20 位更像 OCR 漏识/多识一位，而不是伪造卡。"""
    criteria = detect_boundary_case(
        {"card_number": digits}, _quality(), "review", ["invalid_card_number"]
    )

    assert criteria == [NEAR_MISS_FIELD]


def test_c3_structurally_wrong_card_number_is_not_boundary() -> None:
    """长度差太多是另一类问题，不是「差一点」。"""
    criteria = detect_boundary_case(
        {"card_number": "12345"}, _quality(), "review", ["invalid_card_number"]
    )

    assert criteria == []


def test_c3_month_out_of_range_is_boundary() -> None:
    """平台只校验月份（01-12），年份任意两位都放过 —— 所以越界的只能是月份。"""
    criteria = detect_boundary_case(
        {"valid_date": "13/30"}, _quality(), "review", ["invalid_valid_date"]
    )

    assert criteria == [NEAR_MISS_FIELD]


@pytest.mark.parametrize("value", ["SAD//", "ab/cd", "0130", "13"])
def test_c3_malformed_expiry_shape_is_not_boundary(value: str) -> None:
    """形状就不对的不算近似 —— 那更可能是识别失败而非单字符误识。"""
    criteria = detect_boundary_case(
        {"valid_date": value}, _quality(), "review", ["invalid_valid_date"]
    )

    assert criteria == []


def test_c3_not_applied_to_id_card() -> None:
    """身份证侧没有格式校验（只有 missing_*），C3 无判据可用。"""
    criteria = detect_boundary_case(
        {"valid_date": "01/55"},
        _quality(),
        "review",
        ["invalid_valid_date"],
        doc_type="id_card",
    )

    assert criteria == []


# ── 组合：OR 关系，返回全部命中标签 ─────────────────────────────────────────

def test_criteria_are_ored_and_all_returned() -> None:
    """三类判据是 OR，且要返回全部命中项以便按判据分组统计。"""
    criteria = detect_boundary_case(
        {"valid_date": "13/30"},
        _quality(blur_laplacian_variance=75.0),
        "review",
        ["invalid_valid_date", "glare_detected"],
    )

    assert NEAR_BLUR_THRESHOLD in criteria
    assert NEAR_MISS_FIELD in criteria
    # 两个原因码同时存在 → C2 不该命中（有交叉证据，误报概率低）
    assert FALSE_POSITIVE_CODE not in criteria


def criterion_in(criteria: list[str], expected: str | None) -> bool:
    """只断言目标判据是否命中，不管同一条样本上还命中了什么。

    这些用例的 purpose 是逐轴验证 C1 的带宽，所以传了 ``glare_detected``
    作为原因码（让样本满足「是 review」的前提）—— 那会顺带触发 C2。
    只要断言目标 C1 标签在/不在即可，不要求整个列表为空。
    """
    return (expected in criteria) if expected else (expected not in criteria)
