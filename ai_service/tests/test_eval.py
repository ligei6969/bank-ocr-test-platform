"""评测层的测试：golden 集、四层指标、回归门禁、judge 与校准。

重点不是「数字算得对不对」（那种测试只是把实现抄一遍），而是：

* golden 集的构造是否**可复现、覆盖均衡、doc_type 归一正确**；
* 回归门禁在**方向**上是否正确（越高越好 vs 越低越好），阈值是否卡在 5%；
* judge 的偏差行为是否符合预期（编造阈值要被抓、空答复要得 0）。

其中「指标退化 >5% 时返回告警」是任务书点名的验收项。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ai_service.eval.golden import (
    MAX_GOLDEN_SIZE,
    QUALITY_TYPE_TO_REASONS,
    build_golden_set,
    expected_tools_for,
    load_golden_set,
    normalize_doc_type,
    sample_balance,
)
from ai_service.eval.judge import (
    DETERMINISTIC_ENGINE,
    DeterministicRubricJudge,
    JudgeScore,
    calibrate,
    score_answer,
)
from ai_service.eval.metrics import (
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    METRIC_DIRECTIONS,
    SampleOutcome,
    aggregate,
    compare_to_baseline,
    flatten,
    missing_metrics,
    score_sample,
    unregistered_metrics,
)
from ai_service.eval.report import (
    evaluate,
    load_calibration_cases,
    run_calibration,
    save_baseline,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


FACTS = [
    {
        "code": "image_blur",
        "meaning": "模糊会直接导致 OCR 识别错误。",
        "trigger": "图像清晰度过低。",
        "implementation": "app/quality_check.py 的 detect_blur()，方差小于 80.0 判定为模糊。",
        "advice": "让用户重新拍摄，镜头对准证件保持稳定，避免逆光和抖动。",
        "user_message": "图片有些模糊，请重新拍摄。",
    }
]

GOOD_ANSWER = (
    "本次银行卡审核的结论是「待人工复核」。命中的原因码是 image_blur，"
    "根因在影像质量层。判定依据是 app/quality_check.py 的 detect_blur()，"
    "方差小于 80.0 即判为模糊。处置方向：让用户重新拍摄，镜头对准证件保持稳定，"
    "避免逆光和抖动。给用户的话术：图片有些模糊，请重新拍摄。"
)


# ── golden 集 ─────────────────────────────────────────────────────────────────

def test_doc_type_normalization_maps_label_values_to_service_values() -> None:
    assert normalize_doc_type("bank_card") == "bank_card"
    assert normalize_doc_type("id_card") == "id_card"
    assert normalize_doc_type("id_card_front") == "id_card"
    assert normalize_doc_type("id_card_back") == "id_card"
    # 申请表不在审核链路上，必须排除
    assert normalize_doc_type("application_form") is None
    assert normalize_doc_type("") is None


def test_golden_set_is_deterministic() -> None:
    first = [s.sample_id for s in build_golden_set()]
    second = [s.sample_id for s in build_golden_set()]

    assert first == second
    assert first


def test_golden_set_respects_the_size_cap() -> None:
    assert len(build_golden_set(target_size=999)) <= MAX_GOLDEN_SIZE
    assert len(build_golden_set(target_size=12)) == 12


def test_golden_set_covers_every_mappable_bucket() -> None:
    """轮转取样的目的就是覆盖全部桶；按字母序截断会让后几个桶永远缺席。"""
    samples = build_golden_set(target_size=40)
    buckets = set(sample_balance(samples))

    assert len(buckets) >= 10
    assert {"bank_card/blur", "bank_card/normal", "id_card/glare"} <= buckets


def test_golden_set_only_uses_service_known_doc_types() -> None:
    for sample in build_golden_set():
        assert sample.doc_type in {"bank_card", "id_card"}


def test_golden_set_never_uses_unmapped_quality_types() -> None:
    allowed = set(QUALITY_TYPE_TO_REASONS)

    for sample in build_golden_set():
        assert sample.quality_type in allowed


def test_expected_tools_follow_the_deterministic_sequence() -> None:
    assert expected_tools_for((), None) == ("get_review_record",)
    assert expected_tools_for(("image_blur",), "review") == (
        "get_review_record",
        "search_knowledge",
        "recompute_quality",
    )


def test_expected_reason_codes_come_from_the_documented_mapping() -> None:
    for sample in build_golden_set():
        assert sample.expected_reason_codes == QUALITY_TYPE_TO_REASONS[sample.quality_type]


def test_golden_set_declares_that_the_verdict_layer_is_unavailable() -> None:
    golden = load_golden_set()

    assert golden.verdict_layer_available is False
    assert any("审核结论" in note for note in golden.notes)


def test_golden_summary_reports_its_provenance() -> None:
    summary = load_golden_set().summary()

    assert summary["size"] > 0
    assert summary["labels_path"]
    assert summary["balance"]


# ── 回归门禁（任务书点名的验收项）────────────────────────────────────────────

def test_regression_flags_a_drop_beyond_tolerance() -> None:
    baseline = {"tools.sequence_accuracy": 1.0}
    current = {"tools.sequence_accuracy": 0.94}  # -6%

    alerts = compare_to_baseline(current, baseline)

    assert len(alerts) == 1
    assert alerts[0].metric == "tools.sequence_accuracy"
    assert alerts[0].change < 0
    assert alerts[0].direction == HIGHER_IS_BETTER


def test_regression_ignores_a_drop_within_tolerance() -> None:
    baseline = {"tools.sequence_accuracy": 1.0}
    current = {"tools.sequence_accuracy": 0.96}  # -4%

    assert compare_to_baseline(current, baseline) == []


def test_regression_boundary_is_inclusive() -> None:
    """正好退化 5% 不告警；超过一点就告警。"""
    baseline = {"tools.sequence_accuracy": 1.0}

    assert compare_to_baseline({"tools.sequence_accuracy": 0.95}, baseline) == []
    assert compare_to_baseline({"tools.sequence_accuracy": 0.949}, baseline)


def test_regression_respects_lower_is_better_direction() -> None:
    """平均步数变大是退化，变小是变好 —— 方向搞反门禁就成了摆设。"""
    baseline = {"tools.avg_steps": 2.0}

    worse = compare_to_baseline({"tools.avg_steps": 2.4}, baseline)
    better = compare_to_baseline({"tools.avg_steps": 1.6}, baseline)

    assert len(worse) == 1
    assert worse[0].direction == LOWER_IS_BETTER
    assert better == []


def test_regression_ignores_improvements() -> None:
    baseline = {"tools.sequence_accuracy": 0.5}

    assert compare_to_baseline({"tools.sequence_accuracy": 0.9}, baseline) == []


def test_regression_skips_metrics_absent_from_current() -> None:
    """指标消失不该被当成 0 —— 那会制造一堆假告警，淹没真问题。"""
    baseline = {"tools.sequence_accuracy": 1.0, "task.evidence_rate": 1.0}

    alerts = compare_to_baseline({"tools.sequence_accuracy": 1.0}, baseline)

    assert alerts == []
    assert missing_metrics({"tools.sequence_accuracy": 1.0}, baseline) == ["task.evidence_rate"]


def test_regression_skips_unregistered_metrics() -> None:
    baseline = {"something.experimental": 1.0}

    assert compare_to_baseline({"something.experimental": 0.1}, baseline) == []


def test_unregistered_metrics_are_reported() -> None:
    """算出来但没登记方向的指标不会参与门禁，必须能被发现。"""
    flat = {"tools.sequence_accuracy": 1.0, "brand.new.metric": 0.5}

    assert unregistered_metrics(flat) == ["brand.new.metric"]


def test_custom_tolerance_is_honoured() -> None:
    baseline = {"tools.sequence_accuracy": 1.0}

    assert compare_to_baseline({"tools.sequence_accuracy": 0.9}, baseline, tolerance=0.05)
    assert compare_to_baseline({"tools.sequence_accuracy": 0.9}, baseline, tolerance=0.2) == []


def test_every_emitted_metric_has_a_registered_direction() -> None:
    """新增指标忘了登记方向就会被门禁静默忽略，这条测试专门拦它。"""
    fake_rows = [
        {
            "tools.sequence_match": True,
            "tools.max_sequence_match": True,
            "tools.parameter_ok": True,
            "tools.steps": 3,
            "task.reason_codes_matched": True,
            "task.has_evidence": True,
            "task.evidence_expected": True,
            "task.has_action": True,
            "task.escalation_correct": True,
            "task.degraded": True,
            "task.truncated": False,
            "explain.relevance": 5.0,
            "explain.accuracy": 5.0,
            "explain.completeness": 5.0,
            "explain.usefulness": 5.0,
        }
    ]

    assert unregistered_metrics(flatten(aggregate(fake_rows))) == []


# ── 指标计算 ──────────────────────────────────────────────────────────────────

def test_evidence_rate_ignores_samples_where_evidence_is_not_expected() -> None:
    """没有原因码的记录本来就不该检索，算进分母是惩罚正确行为。"""
    rows = [
        {
            "tools.sequence_match": True,
            "tools.max_sequence_match": True,
            "tools.parameter_ok": True,
            "tools.steps": 1,
            "task.reason_codes_matched": True,
            "task.has_evidence": False,          # 无原因码 => 无引用，正常
            "task.evidence_expected": False,
            "task.has_action": True,
            "task.escalation_correct": True,
            "task.degraded": True,
            "task.truncated": False,
            "explain.relevance": 5.0,
            "explain.accuracy": 5.0,
            "explain.completeness": 5.0,
            "explain.usefulness": 5.0,
        },
        {
            "tools.sequence_match": True,
            "tools.max_sequence_match": True,
            "tools.parameter_ok": True,
            "tools.steps": 3,
            "task.reason_codes_matched": True,
            "task.has_evidence": True,
            "task.evidence_expected": True,
            "task.has_action": True,
            "task.escalation_correct": True,
            "task.degraded": True,
            "task.truncated": False,
            "explain.relevance": 5.0,
            "explain.accuracy": 5.0,
            "explain.completeness": 5.0,
            "explain.usefulness": 5.0,
        },
    ]

    metrics = aggregate(rows)

    assert metrics["task"]["evidence_rate"] == 1.0


def test_aggregate_handles_an_empty_input() -> None:
    metrics = aggregate([])

    assert metrics["count"] == 0
    assert metrics["tools"] == {}


def test_flatten_produces_dotted_keys() -> None:
    flat = flatten({"count": 1, "tools": {"avg_steps": 2.5}, "task": {}, "explain": {}})

    assert flat == {"tools.avg_steps": 2.5}


def test_parameter_accuracy_catches_cross_record_access() -> None:
    """去读别的 request_id 的记录是越权，参数正确率必须抓到。"""
    from ai_service.eval.golden import GoldenSample

    sample = GoldenSample(
        sample_id="golden-x",
        image_path="p",
        doc_type="bank_card",
        quality_type="blur",
        expected_reason_codes=("image_blur",),
        expected_quality_result="review",
        expected_tools=("get_review_record",),
        expects_escalation=False,
    )
    outcome = {
        "trace": [
            {"tool": "get_review_record", "executed": True, "params": {"request_id": "someone-else"}}
        ]
    }

    assert score_sample(SampleOutcome(sample=sample, outcome=outcome))["tools.parameter_ok"] is False


# ── judge ─────────────────────────────────────────────────────────────────────

def test_deterministic_judge_rewards_a_complete_answer() -> None:
    score = DeterministicRubricJudge().score(
        GOOD_ANSWER, context={"review_result": "review"}, facts=FACTS
    )

    assert score.engine == DETERMINISTIC_ENGINE
    assert score.relevance == 5.0
    assert score.accuracy == 5.0
    assert score.usefulness >= 3.0


def test_deterministic_judge_gives_zero_for_an_empty_answer() -> None:
    score = DeterministicRubricJudge().score(
        "", context={"review_result": "review"}, facts=FACTS
    )

    assert score.as_dict() == {
        "relevance": 0.0,
        "accuracy": 0.0,
        "completeness": 0.0,
        "usefulness": 0.0,
    }


def test_deterministic_judge_catches_a_fabricated_threshold() -> None:
    fabricated = (
        "结论是待人工复核，因为 image_blur 命中了。按规则方差小于 120 判为模糊，"
        "误报率约 15%，建议处理。"
    )

    score = DeterministicRubricJudge().score(
        fabricated, context={"review_result": "review"}, facts=FACTS
    )

    assert score.accuracy < 5.0


def test_deterministic_judge_ignores_small_counts() -> None:
    """「共命中 1 个原因码」的 1 是计数不是阈值，不能拿来判准确性。"""
    answer = "结论是「待人工复核」。共命中 1 个原因码，根因在影像质量层。处置方向：让用户重新拍摄。"

    score = DeterministicRubricJudge().score(
        answer, context={"review_result": "review"}, facts=FACTS
    )

    assert score.accuracy == 5.0


def test_deterministic_judge_tolerates_paraphrased_advice() -> None:
    """模型改写措辞不该被判成「没给建议」。"""
    answer = (
        "结论是「待人工复核」，根因在影像质量层。"
        "处置建议是让用户重新拍摄，镜头对准证件保持稳定，避免逆光和抖动。"
    )

    score = DeterministicRubricJudge().score(
        answer, context={"review_result": "review"}, facts=FACTS
    )

    assert score.usefulness >= 3.0


def test_judge_score_is_clamped_to_the_scale() -> None:
    wild = JudgeScore(9.0, -3.0, 4.5, 5.0)

    clamped = wild.clamped()

    assert clamped.relevance == 5.0
    assert clamped.accuracy == 0.0
    assert clamped.completeness == 4.5


def test_score_answer_defaults_to_the_deterministic_judge() -> None:
    score = run(
        score_answer("结论是待人工复核。", context={"review_result": "review"}, facts=FACTS)
    )

    assert score.engine == DETERMINISTIC_ENGINE


# ── 校准 ──────────────────────────────────────────────────────────────────────

def test_calibration_computes_agreement_rates() -> None:
    judged = [JudgeScore(5.0, 4.0, 5.0, 4.0), JudgeScore(3.0, 3.0, 2.0, 1.0)]
    human = [
        {"relevance": 5.0, "accuracy": 4.0, "completeness": 5.0, "usefulness": 4.0},
        {"relevance": 2.0, "accuracy": 3.0, "completeness": 2.0, "usefulness": 1.0},
    ]

    report = calibrate(judged, human)

    assert report.samples == 2
    # 8 个维度里 7 个完全一致
    assert report.exact_match_rate == pytest.approx(7 / 8)
    assert report.within_one_rate == 1.0
    assert report.mean_absolute_error == pytest.approx(1 / 8)
    assert set(report.per_dimension) == {"relevance", "accuracy", "completeness", "usefulness"}


def test_calibration_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError):
        calibrate([JudgeScore(1.0, 1.0, 1.0, 1.0)], [])


def test_calibration_rejects_an_empty_sample() -> None:
    with pytest.raises(ValueError):
        calibrate([], [])


def test_calibration_notes_are_carried_through() -> None:
    report = calibrate(
        [JudgeScore(5.0, 5.0, 5.0, 5.0)],
        [{"relevance": 5.0, "accuracy": 5.0, "completeness": 5.0, "usefulness": 5.0}],
        engine="test",
        notes=["占位标注"],
    )

    assert report.engine == "test"
    assert "占位标注" in report.notes
    assert "一致率" in report.describe()


def test_calibration_cases_exist_and_are_honest_about_being_provisional() -> None:
    cases = load_calibration_cases()

    assert len(cases) >= 10
    for case in cases:
        assert case.get("case_id")
        assert set(case.get("human") or {}) == {
            "relevance",
            "accuracy",
            "completeness",
            "usefulness",
        }


def test_calibration_run_reports_the_engine_and_the_caveat() -> None:
    report = run(run_calibration())

    assert report is not None
    assert report.samples >= 10
    assert 0.0 <= report.within_one_rate <= 1.0
    assert any("占位标注" in note for note in report.notes)


# ── 端到端 ────────────────────────────────────────────────────────────────────

def test_evaluate_runs_offline_and_produces_four_layers() -> None:
    report = run(evaluate(load_golden_set(target_size=8), baseline_path=None))

    assert report.metrics["count"] == 8
    assert set(report.metrics) >= {"count", "tools", "task", "explain"}
    assert report.flat
    assert report.baseline_path is None


def test_evaluate_reports_metrics_without_a_registered_direction() -> None:
    report = run(evaluate(load_golden_set(target_size=8), baseline_path=None))

    assert report.unregistered == []


def test_save_baseline_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    report = run(evaluate(load_golden_set(target_size=8), baseline_path=None))

    save_baseline(report, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["metrics"]
    assert payload["golden"]["size"] == 8
    # 方向表一起存下来，换机器跑时门禁语义不会丢
    assert payload["directions"]


def test_evaluate_against_a_baseline_flags_a_regression(tmp_path: Path) -> None:
    """构造一个「什么都比现在好」的基线，必须报出退化。"""
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps({"metrics": {key: 10.0 for key in ("explain.relevance", "explain.accuracy")}}),
        encoding="utf-8",
    )

    report = run(evaluate(load_golden_set(target_size=8), baseline_path=path))

    assert not report.passed
    assert {alert.metric for alert in report.alerts} == {
        "explain.relevance",
        "explain.accuracy",
    }
    assert report.to_dict()["regression"]["passed"] is False


def test_all_metric_directions_are_declared_for_the_four_layers() -> None:
    for key in (
        "tools.sequence_accuracy",
        "task.reason_code_match_rate",
        "explain.usefulness",
        "cost.avg_tokens",
    ):
        assert key in METRIC_DIRECTIONS


# ── 成本层 ────────────────────────────────────────────────────────────────────

def _golden(sample_id: str = "golden-cost") -> Any:
    from ai_service.eval.golden import GoldenSample

    return GoldenSample(
        sample_id=sample_id,
        image_path="p",
        doc_type="bank_card",
        quality_type="blur",
        expected_reason_codes=("image_blur",),
        expected_quality_result="review",
        expected_tools=("search_knowledge",),
        expects_escalation=False,
    )


def test_score_sample_reads_the_token_usage_block() -> None:
    outcome = {
        "trace": [],
        "token_usage": {
            "total": 300,
            "provider_total": 300,
            "source": "provider",
        },
    }

    row = score_sample(SampleOutcome(sample=_golden(), outcome=outcome))

    assert row["cost.tokens"] == 300
    assert row["cost.provider_tokens"] == 300
    assert row["cost.usage_reported"] is True
    assert row["cost.ran_llm"] is True


def test_score_sample_treats_a_missing_token_block_as_no_model_cost() -> None:
    """P1.5 之前的结果没有这个字段 —— 那是「没调模型」，不是「数据缺失」。"""
    row = score_sample(SampleOutcome(sample=_golden(), outcome={"trace": []}))

    assert row["cost.tokens"] == 0
    assert row["cost.ran_llm"] is False


def test_provider_usage_rate_only_counts_runs_that_called_a_model() -> None:
    """离线样本不算进分母：它们本来就没有用量，算进去是惩罚正确行为。"""
    rows = [
        {"cost.usage_reported": True, "cost.ran_llm": True},
        {"cost.usage_reported": False, "cost.ran_llm": True},
        {"cost.usage_reported": False, "cost.ran_llm": False},
    ]

    rate = aggregate(_with_required_metrics(rows))["cost"]["provider_usage_rate"]

    assert rate == 0.5


def test_cost_average_tokens_is_summed_over_every_sample() -> None:
    rows = [
        {"cost.tokens": 100, "cost.provider_tokens": 100},
        {"cost.tokens": 300, "cost.provider_tokens": 0},
    ]

    metrics = aggregate(_with_required_metrics(rows))

    assert metrics["cost"]["avg_tokens"] == 200.0
    assert metrics["cost"]["avg_provider_tokens"] == 50.0


def _with_required_metrics(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """补上 aggregate 会读的其余字段，让用例只聚焦成本层。"""
    template: Dict[str, Any] = {
        "tools.steps": 2,
        "tools.sequence_match": True,
        "tools.max_sequence_match": True,
        "tools.parameter_ok": True,
        "task.reason_codes_matched": True,
        "task.has_evidence": True,
        "task.evidence_expected": True,
        "task.has_action": True,
        "task.escalation_correct": True,
        "task.degraded": False,
        "task.truncated": False,
        "explain.relevance": 5.0,
        "explain.accuracy": 5.0,
        "explain.completeness": 5.0,
        "explain.usefulness": 5.0,
    }
    return [{**template, **row} for row in rows]


def test_cost_regression_fires_when_token_use_grows() -> None:
    """成本是越低越好：涨了才算退化，降了不该报警。"""
    baseline = {"cost.avg_tokens": 1000.0}

    worse = compare_to_baseline({"cost.avg_tokens": 1200.0}, baseline)
    better = compare_to_baseline({"cost.avg_tokens": 800.0}, baseline)

    assert [alert.metric for alert in worse] == ["cost.avg_tokens"]
    assert worse[0].direction == LOWER_IS_BETTER
    assert better == []


def test_cost_metrics_reach_the_baseline_gate() -> None:
    """成本指标必须能在 baseline 里保存并被门禁读到，否则等于没接。"""
    report = run(evaluate(load_golden_set(target_size=4), baseline_path=None))

    assert "cost.avg_tokens" in report.flat
    assert "cost.provider_usage_rate" in report.flat
    assert report.unregistered == []
