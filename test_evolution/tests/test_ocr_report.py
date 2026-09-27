"""OCR 错误率报告的测试。

这个报告是**阈值决策的输入**，所以它的测试重点不是「能生成表格」，
而是**三个失败原因必须分对**：

* 值不符 —— 认出来了但认错了
* 解析缺失 / 解析器责任 —— 文本里有证据却没取到
* 解析缺失 / OCR 限制 —— 文本里根本没证据

分错的代价在 CTE-3 付过一次：把 OCR 的局限算成解析器的账，
会让验收标准定错（以为要修到 10/10，实际上限是 2/10）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_evolution.ocr_report import (
    BucketReport,
    FieldStats,
    blocked_surface_evidence,
    build_report,
    render_csv,
    render_markdown,
)
from test_evolution.ocr_snapshot import OcrObservation, OcrSnapshot, _key


def _snapshot(entries) -> OcrSnapshot:
    """从 ``[(image_path, doc_type, ocr_texts, parsed_fields)]`` 构造快照。"""
    observations = {}
    for path, doc_type, texts, parsed in entries:
        obs = OcrObservation(
            image_path=_key(path),
            image_sha256="x",
            ocr_texts=list(texts),
            parsed_fields=dict(parsed),
            quality={},
            engine="test",
            engine_version="test",
            recorded_at="",
            doc_type=doc_type,
        )
        observations[_key(path)] = obs
    return OcrSnapshot(observations=observations)


@pytest.fixture()
def labels_file(tmp_path):
    """一份最小的标注集，覆盖三种失败各一例。"""
    payload = [
        {
            "image_path": "data/processed/bank_card/normal/bank_card_0001.png",
            "doc_type": "bank_card",
            "quality_type": "normal",
            "fields": {"card_number": "6222020202020001", "name": "ZHANG SAN", "valid_date": "12/30"},
        },
        {
            "image_path": "data/processed/bank_card/blur/bank_card_0001.png",
            "doc_type": "bank_card",
            "quality_type": "blur",
            "fields": {"card_number": "6222020202020002", "name": "LI LEI", "valid_date": "01/25"},
        },
        {
            "image_path": "data/processed/bank_card/blur/bank_card_0002.png",
            "doc_type": "bank_card",
            "quality_type": "blur",
            "fields": {"card_number": "6222020202020003", "name": "WANG TAO", "valid_date": "02/26"},
        },
    ]
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ── 三种失败要分对 ────────────────────────────────────────────────────────────

def test_a_correct_field_counts_as_correct(labels_file):
    snapshot = _snapshot([
        (
            "data/processed/bank_card/normal/bank_card_0001.png", "bank_card",
            ["TEST BANK 6222 0202 0202 0001", "ZHANG SAN", "VALID THRU 12/30"],
            {"card_number": "6222020202020001", "name": "ZHANG SAN", "valid_date": "12/30"},
        ),
    ])

    buckets = build_report(snapshot, labels_path=labels_file)
    stats = buckets["bank_card/normal"].fields["card_number"]

    assert stats.correct == 1
    assert stats.accuracy == 1.0


def test_a_wrong_value_is_not_counted_as_missing(labels_file):
    """认出来了但认错了 —— 是「值不符」，不是「缺失」。

    混淆这两个会让错误率看起来比实际高（或低），
    也让归因指向错误的方向。
    """
    snapshot = _snapshot([
        (
            "data/processed/bank_card/normal/bank_card_0001.png", "bank_card",
            ["6222 0202 0202 9999"],
            {"card_number": "6222020202029999"},  # 与真值不同
        ),
    ])

    buckets = build_report(snapshot, labels_path=labels_file)
    stats = buckets["bank_card/normal"].fields["card_number"]

    assert stats.wrong_value == 1
    assert stats.missing == 0
    assert stats.accuracy == 0.0


def test_a_missing_field_with_evidence_is_the_parsers_fault(labels_file):
    """文本里有卡号却没解析出来 —— 归解析器，不归 OCR。"""
    snapshot = _snapshot([
        (
            "data/processed/bank_card/blur/bank_card_0001.png", "bank_card",
            ["TEST BANK 6222 0202 0202 0002"],  # 文本里有
            {},  # 但没解析出来
        ),
    ])

    buckets = build_report(snapshot, labels_path=labels_file)
    stats = buckets["bank_card/blur"].fields["card_number"]

    assert stats.missing_with_evidence == 1
    assert stats.missing_without_evidence == 0


def test_a_missing_field_without_evidence_is_an_ocr_limit(labels_file):
    """文本里根本没有那串数字 —— 归 OCR，不归解析器。

    这是 CTE-2 快照里 8/10 的那种情况：修解析器不会让这个数字变好。
    """
    snapshot = _snapshot([
        (
            "data/processed/bank_card/blur/bank_card_0001.png", "bank_card",
            ["TEST BANK", "VALID THRU"],  # 没有卡号
            {},
        ),
    ])

    buckets = build_report(snapshot, labels_path=labels_file)
    stats = buckets["bank_card/blur"].fields["card_number"]

    assert stats.missing_without_evidence == 1
    assert stats.missing_with_evidence == 0


# ── 归一 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "parsed,expected",
    [
        ("6222020202020001", "6222 0202 0202 0001"),  # 空格
        ("6222-0202-0202-0001", "6222020202020001"),  # 连字符
        ("6222020202020001", "6222020202020001"),  # 完全一致
    ],
)
def test_formatting_differences_are_not_counted_as_errors(
    labels_file, parsed, expected
):
    """格式差异不该算识别错误 —— 否则报告测的是格式而非能力。

    只针对 ``card_number`` 一个字段做断言：真值里另外两个字段
    没有对应的解析结果，会把它们算成「缺失」而干扰这条测试的判据。
    """
    snapshot = _snapshot([
        (
            "data/processed/bank_card/normal/bank_card_0001.png", "bank_card",
            [parsed], {"card_number": parsed},
        ),
    ])

    buckets = build_report(snapshot, labels_path=labels_file)
    stats = buckets["bank_card/normal"].fields["card_number"]

    assert stats.correct == 1, f"{parsed!r} 与 {expected!r} 应判为一致"


def test_name_comparison_ignores_case(labels_file):
    """姓名比对不区分大小写 —— 真实 OCR 给的是全大写，标注是混合大小写。"""
    snapshot = _snapshot([
        (
            "data/processed/bank_card/normal/bank_card_0001.png", "bank_card",
            ["ZHANG SAN"], {"name": "ZHANG SAN"},
        ),
    ])

    buckets = build_report(snapshot, labels_path=labels_file)
    stats = buckets["bank_card/normal"].fields["name"]

    assert stats.correct == 1


# ── 覆盖与边界 ────────────────────────────────────────────────────────────────

def test_images_absent_from_the_snapshot_are_skipped_not_counted_as_failures(labels_file):
    """没录进快照的样本不该被算成失败 —— 那是「没测」，不是「测了没过」。"""
    snapshot = _snapshot([
        ("data/processed/bank_card/normal/bank_card_0001.png", "bank_card", ["x"], {}),
    ])

    buckets = build_report(snapshot, labels_path=labels_file)

    assert "bank_card/normal" in buckets
    assert "bank_card/blur" not in buckets, "blur 桶里一张也没录，不该出现"


def test_buckets_are_split_by_quality_type(labels_file):
    snapshot = _snapshot([
        ("data/processed/bank_card/blur/bank_card_0001.png", "bank_card", ["x"], {}),
        ("data/processed/bank_card/blur/bank_card_0002.png", "bank_card", ["x"], {}),
    ])

    buckets = build_report(snapshot, labels_path=labels_file)

    assert buckets["bank_card/blur"].total == 2
    assert buckets["bank_card/blur"].doc_type == "bank_card"
    assert buckets["bank_card/blur"].quality_type == "blur"


# ── 渲染 ──────────────────────────────────────────────────────────────────────

def test_markdown_report_states_the_three_categories_are_separate(labels_file):
    """报告必须写明三列含义 —— 否则读者会把「解析缺失」当成一个数。"""
    snapshot = _snapshot([
        ("data/processed/bank_card/blur/bank_card_0001.png", "bank_card", ["TEST BANK"], {}),
    ])
    buckets = build_report(snapshot, labels_path=labels_file)

    md = render_markdown(buckets, snapshot=snapshot)

    assert "解析器责任" in md
    assert "OCR 限制" in md
    assert "值不符" in md


def test_csv_has_a_row_per_field(labels_file):
    snapshot = _snapshot([
        ("data/processed/bank_card/blur/bank_card_0001.png", "bank_card", ["TEST BANK"], {}),
    ])
    buckets = build_report(snapshot, labels_path=labels_file)

    csv_text = render_csv(buckets)
    lines = [line for line in csv_text.strip().splitlines() if line]

    assert lines[0].startswith("doc_type,quality_type,field")
    assert any("card_number" in line for line in lines[1:])


def test_report_is_generated_only_from_the_snapshot_and_labels(labels_file):
    """同一输入必须给出同一报告 —— 它是基线，不能有随机性或时间戳漂移。"""
    snapshot = _snapshot([
        ("data/processed/bank_card/blur/bank_card_0001.png", "bank_card", ["TEST BANK"], {}),
    ])

    first = render_csv(build_report(snapshot, labels_path=labels_file))
    second = render_csv(build_report(snapshot, labels_path=labels_file))

    assert first == second


# ── 给 readiness 的证据摘要 ───────────────────────────────────────────────────

def test_evidence_summary_reports_per_bucket_rates(labels_file):
    snapshot = _snapshot([
        ("data/processed/bank_card/blur/bank_card_0001.png", "bank_card", ["TEST BANK"], {}),
        ("data/processed/bank_card/blur/bank_card_0002.png", "bank_card",
         ["6222 0202 0202 0003", "WANG TAO", "02/26"],
         {"card_number": "6222020202020003", "name": "WANG TAO", "valid_date": "02/26"}),
    ])
    buckets = build_report(snapshot, labels_path=labels_file)

    evidence = blocked_surface_evidence(buckets)

    assert evidence["bank_card/blur"]["samples"] == 2
    assert 0.0 <= evidence["bank_card/blur"]["mean_field_accuracy"] <= 1.0
    assert 0.0 <= evidence["bank_card/blur"]["missing_rate"] <= 1.0


def test_evidence_summary_does_not_recommend_thresholds(labels_file):
    """摘要只汇总事实，不给阈值建议 —— 那是产品口径决策，不是脚本的事。"""
    snapshot = _snapshot([
        ("data/processed/bank_card/blur/bank_card_0001.png", "bank_card", ["TEST BANK"], {}),
    ])
    buckets = build_report(snapshot, labels_path=labels_file)

    evidence = blocked_surface_evidence(buckets)

    for item in evidence.values():
        assert set(item) == {"samples", "mean_field_accuracy", "missing_rate"}


# ── FieldStats 自身 ───────────────────────────────────────────────────────────

def test_field_stats_missing_is_the_sum_of_both_causes():
    stats = FieldStats(total=10, missing_with_evidence=3, missing_without_evidence=2)

    assert stats.missing == 5


def test_accuracy_is_none_when_nothing_was_measured():
    """零样本不该报 0% —— 那会被读成「全错」。"""
    assert FieldStats().accuracy is None


def test_bucket_with_no_fields_reports_zero_samples():
    assert BucketReport("bank_card", "blur").total == 0
