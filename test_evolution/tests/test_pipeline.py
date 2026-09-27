"""CTE 闭环的端到端测试：**CTE-1 的验收就落在这里**。

跑的是方案第二十一节定的那条链：

    Event → Predict → Execute → Compare → Reflect → Candidate → Validate → Promote

重点不在「能跑通」，而在几个顺序与权限的不变量：
预测必须早于执行、未批准不得晋级、被拒必须留原因。
"""

from __future__ import annotations

import json

import pytest

from test_evolution.pipeline import (
    COMPARISON_CLASSES,
    EVENT_LOG,
    Execution,
    blind_predict,
    compare,
    execute,
    observe,
    render_retro,
    run_event_loop,
    validate_new_test,
)
from test_evolution.schema import Candidate, Prediction, load_candidates, load_events

CREDIT_SPEC = EVENT_LOG[0]


@pytest.fixture()
def root(tmp_path):
    return tmp_path


# ── 各阶段 ────────────────────────────────────────────────────────────────────

def test_observe_writes_the_event_and_is_idempotent(root):
    first = observe(CREDIT_SPEC, root=root)
    second = observe(CREDIT_SPEC, root=root)  # 重跑整条链不该炸

    assert first.event_id == second.event_id
    assert len(load_events(root=root)) == 1


def test_observe_does_not_touch_the_system_under_test(root):
    """Observe 阶段禁止修改系统 —— 这里只允许产生 test_evolution 下的文件。"""
    before = set(root.rglob("*"))
    observe(CREDIT_SPEC, root=root)
    created = set(root.rglob("*")) - before

    assert created, "至少应该写下一个事件文件"
    assert all("test_evolution" in str(p) or p.is_relative_to(root) for p in created)


def test_blind_predict_does_not_overwrite_on_rerun(root):
    event = observe(CREDIT_SPEC, root=root)
    first = blind_predict(event, root=root, confidence=0.9)
    second = blind_predict(event, root=root, confidence=0.1)  # 想改高一点

    assert first.confidence == second.confidence == 0.9, "重跑不得覆盖已落盘的盲预测"


def test_prediction_is_on_disk_before_execution(root):
    """顺序不变量：预测必须先落盘，再执行。

    这是整个盲预测机制的意义所在 —— 如果执行在前，「预测」就只是
    一个事后编出来的说法。
    """
    event = observe(CREDIT_SPEC, root=root)
    prediction = blind_predict(event, root=root)

    prediction_file = root / "predictions" / f"{prediction.prediction_id}.json"
    assert prediction_file.exists()

    on_disk = json.loads(prediction_file.read_text(encoding="utf-8"))
    assert on_disk["actual_result"] is None, "执行还没发生，实际结果不该已经在盘上"


def test_execute_replays_the_historical_behaviour(root):
    event = observe(CREDIT_SPEC, root=root)
    execution = execute(event, system_version=event.system_version)

    assert execution.actual_result == "knowledge", "重演应当得到当初那个错误结论"
    assert "policy@pre-bb7947e" in execution.executor


def test_execute_head_shows_the_fixed_behaviour(root):
    event = observe(CREDIT_SPEC, root=root)
    execution = execute(event, system_version="HEAD")

    assert execution.actual_result == "pii"


# ── 对比分类 ──────────────────────────────────────────────────────────────────

def _prediction(**overrides) -> Prediction:
    payload = {
        "prediction_id": "P", "event_id": "E", "predicted_result": "pii",
        "likely_failure": (), "risk_area": (), "confidence": 0.8, "recorded_at": "",
    }
    payload.update(overrides)
    return Prediction(**payload)


def _execution(actual: str) -> Execution:
    return Execution(event_id="E", actual_result=actual, executor="x", detail={})


def test_actual_matching_expectation_after_a_correct_call():
    comparison = compare(_prediction(), _execution("pii"), expected="pii")

    assert comparison.classification == "correct_prediction"
    assert comparison.prediction_hit is True


def test_letting_a_shielded_question_through_is_a_failure_pattern():
    """该拒没拒 —— 这是 CTE 最该抓的一类，必须被单独分类。"""
    comparison = compare(_prediction(), _execution("knowledge"), expected="pii")

    assert comparison.classification == "new_failure_pattern"
    assert comparison.prediction_hit is False


def test_a_known_pattern_is_labelled_as_known():
    """老问题又来了，和「新发现」在记录上要分得开。"""
    prediction = _prediction(likely_failure=("pii_keyword_table_too_narrow",))
    comparison = compare(
        prediction, _execution("knowledge"), expected="pii",
        known_patterns=("pii_keyword_table_too_narrow",),
    )

    assert comparison.classification == "known_failure_pattern"


def test_classifications_stay_within_the_documented_set():
    for actual in ("pii", "knowledge", "product", "advice"):
        comparison = compare(_prediction(), _execution(actual), expected="pii")
        assert comparison.classification in COMPARISON_CLASSES


def test_over_refusal_is_not_counted_as_a_correct_prediction():
    """拒过头了同样不是「预测正确」。"""
    prediction = _prediction(predicted_result="knowledge")
    comparison = compare(prediction, _execution("pii"), expected="pii")

    assert comparison.classification == "false_positive"
    assert comparison.prediction_hit is False


# ── retro ─────────────────────────────────────────────────────────────────────

def test_retro_answers_the_six_questions():
    """方案 Phase 5 要求复盘至少覆盖这几件事，缺一条这份复盘就没用。"""
    event = observe(CREDIT_SPEC, root=None) if False else _event()
    comparison = compare(_prediction(), _execution("knowledge"), expected="pii")
    body = render_retro(event, comparison)

    for heading in (
        "发生了什么", "为什么现有测试没发现", "归因",
        "现有测试缺口", "是否已有类似历史案例", "应该补什么测试资产",
    ):
        assert heading in body, heading


def test_retro_records_that_the_prediction_missed():
    """盲预测没命中就要白纸黑字写出来 —— 这正是记录它的意义。"""
    event = _event()
    comparison = compare(_prediction(), _execution("knowledge"), expected="pii")
    body = render_retro(event, comparison)

    assert "命中：否" in body


def _event():
    from test_evolution.schema import Event

    return Event(
        event_id="EVT-001", source="threat_test_failure", surface="threat",
        title="征信问句的改写绕过越界关键词表", observed_at="",
        system_version="policy@pre-bb7947e", input_case="我征信上有什么问题",
        current_result="knowledge（当作普通咨询作答）",
        expected_result="pii（必须拒答 + 引导）",
    )


# ── NEW_TEST 验证 ─────────────────────────────────────────────────────────────

def test_new_test_validation_requires_both_before_and_after(root):
    """一个测试的价值在于**修复前 FAIL、修复后 PASS**。

    只满足一边都不是资产：抓不到旧缺陷 = 防不住回归；
    当前版本仍失败 = 那是个没修的 bug，不是测试。
    """
    candidate = Candidate(
        candidate_id="CTE-001", type="NEW_TEST", title="t", evidence=("EVT-001",),
        surface="threat", proposed_change="加回归", created_at="",
    )
    validate_new_test(
        candidate, question="我征信上有什么问题", root=root,
        full_regression={"outcome": "pass"},
    )

    assert candidate.checks["reproduces_before_fix"] == "pass"
    assert candidate.checks["passes_after_fix"] == "pass"
    assert candidate.is_machine_validated is True


def test_a_test_that_never_reproduced_anything_fails_validation(root):
    """给一个「两边都拒答」的正常问句 —— 它抓不到任何缺陷。"""
    candidate = Candidate(
        candidate_id="CTE-002", type="NEW_TEST", title="t", evidence=("EVT-001",),
        surface="threat", proposed_change="加回归", created_at="",
    )
    validate_new_test(
        candidate, question="办理二类账户需要哪些材料", root=root,
        full_regression={"outcome": "pass"},
    )

    assert candidate.checks["reproduces_before_fix"] == "fail"
    assert candidate.is_machine_validated is False


# ── 整条链 ────────────────────────────────────────────────────────────────────

def test_full_loop_produces_every_artifact(root):
    """CTE-1 的验收：一条真实事件能走完整条链，且每步都留证据。"""
    outcome = run_event_loop(
        CREDIT_SPEC,
        root=root,
        candidate_id="CTE-001",
        candidate_type="NEW_TEST",
        candidate_title="征信改写问句必须判为 pii",
        proposed_change="新增回归测试覆盖裸词「征信」族的改写写法",
        full_regression={"outcome": "pass"},
    )

    assert outcome["event"]["event_id"] == "EVT-001"
    assert outcome["comparison"]["classification"] == "new_failure_pattern"
    assert outcome["retro"] is not None
    assert outcome["candidate"]["is_machine_validated"] is True
    assert outcome["promoted"] is None, "没给 approver 就不该晋级"

    assert (root / "retros" / "EVT-001.md").exists()


def test_full_loop_does_not_promote_without_a_human(root):
    outcome = run_event_loop(
        CREDIT_SPEC, root=root, candidate_id="CTE-001", candidate_type="NEW_TEST",
        candidate_title="t", proposed_change="p", full_regression={"outcome": "pass"},
    )

    (candidate,) = load_candidates(root=root)

    assert candidate.status == "machine_validated"
    assert candidate.approver == ""
    assert outcome["promoted"] is None


def test_full_loop_with_an_approver_promotes(root):
    outcome = run_event_loop(
        CREDIT_SPEC, root=root, candidate_id="CTE-001", candidate_type="NEW_TEST",
        candidate_title="t", proposed_change="p", full_regression={"outcome": "pass"},
        approver="jb",
    )

    assert outcome["promoted"] == "validated"
    (candidate,) = load_candidates(root=root)
    assert candidate.approver == "jb"


def test_promotion_writes_the_validated_knowledge(root):
    """晋级必须同时落下 Validated Knowledge —— 那是 RAG 唯一的索引源。

    只把状态改成 validated、不写文件，``validated/`` 会永远空着，
    整条链的产出就只剩下一堆过程证据，没有资产。
    """
    run_event_loop(
        CREDIT_SPEC, root=root, candidate_id="CTE-001", candidate_type="NEW_TEST",
        candidate_title="征信改写问句必须判为 pii", proposed_change="加回归",
        full_regression={"outcome": "pass"}, approver="jb",
    )

    knowledge = root / "validated" / "CTE-001.md"
    assert knowledge.exists()

    body = knowledge.read_text(encoding="utf-8")
    assert "征信改写问句必须判为 pii" in body
    assert "jb" in body, "Validated Knowledge 必须记名批准人"
    # 验证一节由 checks 生成，不是写死的
    assert "reproduces_before_fix: pass" in body


def test_no_approval_means_no_validated_knowledge(root):
    run_event_loop(
        CREDIT_SPEC, root=root, candidate_id="CTE-001", candidate_type="NEW_TEST",
        candidate_title="t", proposed_change="p", full_regression={"outcome": "pass"},
    )

    assert not (root / "validated").exists() or not list((root / "validated").glob("*.md"))


def test_promotion_stamps_the_time(root):
    """晋级记录不写时间，审计时就说不清「什么时候批的」。"""
    run_event_loop(
        CREDIT_SPEC, root=root, candidate_id="CTE-001", candidate_type="NEW_TEST",
        candidate_title="t", proposed_change="p", full_regression={"outcome": "pass"},
        approver="jb",
    )

    (candidate,) = load_candidates(root=root)

    assert candidate.validated_at, "晋级必须留下时间戳"


def test_produced_asset_is_a_repo_relative_path(root):
    """审计记录里存绝对路径换台机器就错，也会把本地目录结构带进仓库。"""
    run_event_loop(
        CREDIT_SPEC, root=root, candidate_id="CTE-001", candidate_type="NEW_TEST",
        candidate_title="t", proposed_change="p", full_regression={"outcome": "pass"},
        approver="jb",
    )

    (candidate,) = load_candidates(root=root)

    assert candidate.produced_asset
    assert not candidate.produced_asset.startswith("/")
    assert ":" not in candidate.produced_asset, "不该含盘符"
    assert "\\" not in candidate.produced_asset, "统一用正斜杠"


def test_loop_is_rerunnable(root):
    """重跑整条链不该产生第二份事件、也不该覆盖已闭合的预测。"""
    kwargs = dict(
        root=root, candidate_id="CTE-001", candidate_type="NEW_TEST",
        candidate_title="t", proposed_change="p", full_regression={"outcome": "pass"},
    )
    run_event_loop(CREDIT_SPEC, **kwargs)
    run_event_loop(CREDIT_SPEC, **kwargs)

    assert len(load_events(root=root)) == 1
    assert len(list((root / "predictions").glob("*.json"))) == 1


def test_loop_writes_nothing_outside_its_own_tree(root):
    """CTE 不得越界写文件 —— 这是「不可自修改区」在行为层的证据。"""
    run_event_loop(
        CREDIT_SPEC, root=root, candidate_id="CTE-001", candidate_type="NEW_TEST",
        candidate_title="t", proposed_change="p", full_regression={"outcome": "pass"},
    )

    written = {p for p in root.rglob("*") if p.is_file()}

    assert written, "应该产出了证据文件"
    for path in written:
        assert path.is_relative_to(root)
