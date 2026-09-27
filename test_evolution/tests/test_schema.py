"""CTE schema 的契约测试。

这些测试守的不是「代码能跑」，而是**方案里那些不变量**：
预测不可覆盖、没有证据不得成候选、机器验证不等于可以晋级、
拒绝必须留原因。每一条都对应方案里的一句要求，砍掉任何一条
CTE 就退化成「一个会给自己发合格证的文件目录」。
"""

from __future__ import annotations

import json

import pytest

from test_evolution.schema import (
    Candidate,
    Event,
    Prediction,
    SchemaError,
    apply_checks,
    load_candidates,
    promote,
    validation_report,
    write_candidate,
    write_event,
    write_prediction,
    write_rejected,
    write_retro,
    write_validated,
)


@pytest.fixture()
def root(tmp_path):
    return tmp_path


# ── Event ─────────────────────────────────────────────────────────────────────

def test_event_rejects_unknown_source():
    with pytest.raises(SchemaError, match="未知事件来源"):
        Event(
            event_id="E1", source="随手编的", surface="threat", title="t",
            observed_at="", system_version="", input_case="", current_result="",
            expected_result="",
        )


def test_learning_blocked_follows_the_readiness_table():
    """``learning_blocked`` 必须跟着就绪度表走，不能自己藏一份名单。

    早先 ``BLOCKED_SURFACES`` 是硬编码的 ``{"ocr", "adjudication"}``；
    CTE-2 交付 OCR 快照后它就成了过期常量，会让事件被无理由地拒绝。
    """
    from test_evolution.readiness import learning_allowed

    for name in ("knowledge", "threat", "agent", "ocr", "adjudication"):
        event = Event(
            event_id="E", source="new_bug", surface=name, title="t", observed_at="",
            system_version="", input_case="", current_result="", expected_result="",
        )
        assert event.learning_blocked is (not learning_allowed(name)), name


def test_cte2_unblocked_the_ocr_surface():
    """CTE-2 之后 OCR 不再 blocked —— 事件可以走完整闭环（CTE-3）。"""
    from test_evolution.schema import BLOCKED_SURFACES

    assert "ocr" not in BLOCKED_SURFACES
    assert BLOCKED_SURFACES == frozenset(), "快照交付后不该还有 blocked 的面"


# ── Prediction：盲预测不可覆盖 ────────────────────────────────────────────────

def test_prediction_cannot_be_overwritten_on_disk(root):
    prediction = Prediction(
        prediction_id="P1", event_id="E1", predicted_result="pii", likely_failure=(),
        risk_area=(), confidence=0.5, recorded_at="",
    )
    write_prediction(prediction, root=root)

    with pytest.raises(FileExistsError, match="只允许追加"):
        write_prediction(prediction, root=root)


def test_a_closed_prediction_cannot_be_re_closed():
    """闭合后改实际结果 = 篡改记录。这是「不许事后声称我早知道」的落点。"""
    prediction = Prediction(
        prediction_id="P1", event_id="E1", predicted_result="pii", likely_failure=(),
        risk_area=(), confidence=0.5, recorded_at="",
    )
    prediction.record_outcome("knowledge")

    with pytest.raises(SchemaError, match="已经闭合"):
        prediction.record_outcome("pii")


def test_unresolved_prediction_reports_none_not_false():
    """未闭合的预测不参与准确率统计 —— 返回 None，不返回 False。

    返回 False 会让「还没跑」和「预测错了」在统计里混成一件事。
    """
    prediction = Prediction(
        prediction_id="P1", event_id="E1", predicted_result="pii", likely_failure=(),
        risk_area=(), confidence=0.5, recorded_at="",
    )

    assert prediction.hit is None
    assert prediction.is_resolved is False


def test_confidence_must_be_a_probability():
    with pytest.raises(SchemaError, match="confidence"):
        Prediction(
            prediction_id="P1", event_id="E1", predicted_result="pii",
            likely_failure=(), risk_area=(), confidence=1.5, recorded_at="",
        )


def test_comparison_without_result_is_rejected():
    with pytest.raises(SchemaError, match="比对必须先有结果"):
        Prediction(
            prediction_id="P1", event_id="E1", predicted_result="pii",
            likely_failure=(), risk_area=(), confidence=0.5, recorded_at="",
            comparison="known_failure_pattern",
        )


# ── Candidate：证据是硬门槛 ───────────────────────────────────────────────────

def _candidate(**overrides) -> Candidate:
    payload = {
        "candidate_id": "CTE-001",
        "type": "NEW_TEST",
        "title": "t",
        "evidence": ("EVT-001",),
        "surface": "threat",
        "proposed_change": "加一条回归测试",
        "created_at": "",
    }
    payload.update(overrides)
    return Candidate(**payload)


def test_candidate_without_evidence_is_rejected():
    """方案 Risk 4：没有证据支持的 Reflection 不得生成 Candidate。

    这是 Candidate 数量爆炸的解药 —— 在构造函数里挡住，不靠评审时拦。
    """
    with pytest.raises(SchemaError, match="没有任何 evidence"):
        _candidate(evidence=())


def test_unknown_candidate_type_is_rejected():
    with pytest.raises(SchemaError, match="未知 Candidate 类型"):
        _candidate(type="VIBES_UPDATE")


def test_each_type_has_a_validation_matrix_entry():
    """不允许出现「有类型但不知道该怎么验」的空档。"""
    from test_evolution.schema import CANDIDATE_TYPES, VALIDATION_MATRIX

    for candidate_type in CANDIDATE_TYPES:
        assert candidate_type in VALIDATION_MATRIX, candidate_type
        assert VALIDATION_MATRIX[candidate_type], candidate_type


# ── 晋级门：机器验证 ≠ 可以晋级 ───────────────────────────────────────────────

def test_checks_outside_the_matrix_are_rejected():
    candidate = _candidate()

    with pytest.raises(SchemaError, match="不在 .* 的验证矩阵里"):
        apply_checks(candidate, {"human_review": "pass"})


def test_skipped_is_not_a_pass(root):
    """「这项没测」和「这项测了没过」要可区分，但都挡住晋级。"""
    candidate = _candidate()
    apply_checks(
        candidate,
        {"executable": "pass", "reproduces_before_fix": "skipped",
         "passes_after_fix": "pass", "full_regression": "pass"},
    )

    assert candidate.is_machine_validated is False
    assert candidate.can_promote is False


def test_machine_validated_still_cannot_promote_without_a_human():
    """方案第十一节条件 6：不存在 machine_validated → production 的直达路径。"""
    candidate = _candidate(type="FAILURE_PATTERN", evidence=("EVT-001", "EVT-002"))
    apply_checks(candidate, {"has_evidence": "pass", "history_verified": "pass"})

    assert candidate.is_machine_validated is True
    assert candidate.needs_human_review is True
    assert candidate.can_promote is False, "机器过关但没人签字，仍然晋级不了"

    with pytest.raises(SchemaError, match="机器验证未全部通过|须署名"):
        promote(candidate, approver="", root=root)


def test_every_candidate_type_requires_human_approval():
    """没有例外：任何类型的 Candidate 都不能凭机器验证自己晋级。

    这条守的是整份方案最核心的那句边界。一旦某个类型能自动晋级，
    CTE 就变回「一个会自己发合格证的系统」。
    """
    from test_evolution.schema import CANDIDATE_TYPES

    for candidate_type in CANDIDATE_TYPES:
        candidate = _candidate(type=candidate_type, evidence=("EVT-001",))
        assert candidate.needs_human_review is True, candidate_type
        assert candidate.can_promote is False, candidate_type


def test_risk_rule_can_never_be_promoted(root):
    """方案第十二节：``RISK_RULE`` 只能形成提案，禁止自动 Promotion 到 production。"""
    candidate = _candidate(type="RISK_RULE", evidence=("EVT-001",))
    apply_checks(candidate, {"proposal_only": "pass"})

    assert candidate.is_proposal_only is True
    assert candidate.can_promote is False

    with pytest.raises(SchemaError, match="只能提案"):
        promote(candidate, approver="jb", root=root)


def test_proposal_only_is_not_counted_as_a_machine_check():
    """``proposal_only`` 不是「跑一遍就知道过没过」的检查，不该算进机器验证。"""
    candidate = _candidate(type="RISK_RULE", evidence=("EVT-001",))
    apply_checks(candidate, {"proposal_only": "pass"})

    assert candidate.is_machine_validated is True
    assert candidate.is_proposal_only is True


def test_promotion_requires_a_signature(root):
    candidate = _candidate(type="THREAT_CASE")
    apply_checks(
        candidate,
        {"threat_runner": "pass", "expected_policy_verdict": "pass",
         "full_regression": "pass"},
    )

    with pytest.raises(SchemaError, match="必须署名"):
        promote(candidate, approver="", root=root)

    promote(candidate, approver="jb", root=root)
    assert candidate.status == "validated"


def test_validated_asset_cannot_be_written_before_promotion(root):
    """``validated/`` 是未来 RAG 的唯一索引源 —— 没签字的东西进不去。"""
    candidate = _candidate(type="THREAT_CASE")
    apply_checks(candidate, {"threat_runner": "pass", "expected_policy_verdict": "pass",
                             "full_regression": "pass"})

    with pytest.raises(SchemaError, match="不满足晋级条件"):
        write_validated(candidate, "# 内容", root=root)


def test_rejection_must_carry_a_reason(root):
    """方案第 27 节：rejected Candidate 保留原因。

    被拒的提议也是资产 —— 它记录「这条路试过、不行、为什么」。
    """
    candidate = _candidate()

    with pytest.raises(SchemaError, match="必须写明 reason"):
        write_rejected(candidate, reason="", root=root)

    path = write_rejected(candidate, reason="该写法已被现有用例覆盖", root=root)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["status"] == "rejected"
    assert payload["rejection_reason"] == "该写法已被现有用例覆盖"


def test_retro_is_unique_per_event(root):
    write_retro("EVT-001", "# 复盘", root=root)

    with pytest.raises(FileExistsError, match="已存在"):
        write_retro("EVT-001", "# 改写历史", root=root)


def test_validation_report_lists_pending_checks():
    """报告要能回答「还差哪一步」，而不只是「过没过」。"""
    candidate = _candidate()
    apply_checks(candidate, {"executable": "pass"})
    report = validation_report(candidate)

    assert report["checks"]["executable"] == "pass"
    assert report["checks"]["reproduces_before_fix"] == "pending"
    assert report["can_promote"] is False


def test_roundtrip_through_disk_preserves_the_contract(root):
    candidate = _candidate()
    apply_checks(
        candidate,
        {"executable": "pass", "reproduces_before_fix": "pass",
         "passes_after_fix": "pass", "full_regression": "pass"},
    )
    write_candidate(candidate, root=root)

    (loaded,) = load_candidates(root=root)

    assert loaded.candidate_id == "CTE-001"
    assert loaded.checks == candidate.checks
    assert loaded.is_machine_validated is True


def test_events_are_not_overwritten(root):
    event = Event(
        event_id="EVT-001", source="threat_test_failure", surface="threat",
        title="t", observed_at="", system_version="", input_case="",
        current_result="", expected_result="",
    )
    write_event(event, root=root)

    with pytest.raises(FileExistsError):
        write_event(event, root=root)
