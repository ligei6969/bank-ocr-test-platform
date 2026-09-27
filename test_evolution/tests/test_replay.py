"""历史重演的测试。

这块是 CTE 里最容易被做假的地方 —— 只要写一句「复现了旧缺陷」，
retro 看起来就完整了。这些测试的存在就是为了让那句话必须能被证伪：
**重演必须在当前正确版本上给出「不拒答」，在当前 HEAD 上给出「拒答」**。
两边都对，才算这个缺陷真的被抓住过。
"""

from __future__ import annotations

import pytest

from test_evolution.replay import (
    POLICY_HISTORY,
    current_behaviour,
    detect_intent_under,
    replay,
    verify_bug_reproduction,
)

CREDIT_QUESTION = "我征信上有什么问题"


# ── 重演确实复现了缺陷 ────────────────────────────────────────────────────────

def test_the_historical_snapshot_reproduces_the_bypass():
    """修复前的规则表下，这句话被判成普通咨询 —— 这就是当初的漏拒。"""
    result = replay(CREDIT_QUESTION)

    assert result.intent == "knowledge"
    assert result.out_of_scope is False


def test_head_refuses_the_same_question():
    """当前版本必须拒答。两边一起为真，才说明修复真的发生过。"""
    result = current_behaviour(CREDIT_QUESTION)

    assert result.out_of_scope is True


def test_verify_bug_reproduction_certifies_the_pair():
    proof = verify_bug_reproduction(CREDIT_QUESTION)

    assert proof["reproduced_before"] is True
    assert proof["fixed_after"] is True
    assert proof["is_regression_test"] is True


def test_the_snapshot_diff_is_the_fix():
    """重演 fixture 与当前规则的差集，恰好就是 bb7947e 那次修复。

    这条断言的价值在于：fixture 一旦被误改成「本来就包含征信」，
    重演会立刻失去复现能力，这里会红。
    """
    from ai_service.knowledge import policy

    current_pii = dict(policy._INTENT_RULES)[policy.INTENT_PII]
    snapshot_pii = POLICY_HISTORY["policy@pre-bb7947e"]["intents"][policy.INTENT_PII]

    missing = set(current_pii) - set(snapshot_pii)

    assert "征信" in missing, "快照里不该有关键词「征信」—— 它正是这次修复加上的"
    assert "我的征信" in snapshot_pii, "快照必须保留当时的关键词，否则不是在重演历史"


# ── 重演的诚实性 ──────────────────────────────────────────────────────────────

def test_replay_declares_its_fidelity():
    """只回放规则层就要说只回放规则层，不能暗示这是完整历史重演。"""
    result = replay(CREDIT_QUESTION)

    assert result.fidelity == "rule_layer_only"
    assert any("prompt" in note for note in result.notes)


def test_unknown_system_version_fails_loudly():
    """宁可报错，也不要静默回退到当前规则 —— 那会记下一个假的历史行为。"""
    with pytest.raises(KeyError, match="没有"):
        replay(CREDIT_QUESTION, "policy@查无此版本")
    with pytest.raises(KeyError):
        verify_bug_reproduction(CREDIT_QUESTION, system_version="policy@v0.0.0")


def test_replay_is_deterministic():
    """同一输入重跑必须逐字段一致 —— 否则 retro 里的结论不可复现。"""
    first = replay(CREDIT_QUESTION).to_dict()
    second = replay(CREDIT_QUESTION).to_dict()

    assert first == second


def test_detection_under_custom_rules_prefers_actionable_intents():
    """顺序即优先级：先判动作性越界，再落到 knowledge。

    这条一旦反了，「我这张卡审核为什么被拒，额度能提多少」
    会被判成普通咨询 —— 正是规则表顺序写错的典型后果。
    """
    rules = {
        "internal": ("审核为什么",),
        "pii": ("额度能提",),
        "advice": (),
    }

    assert detect_intent_under("我这张卡审核为什么被拒，额度能提多少", rules) == "internal"


def test_a_question_matching_nothing_falls_through_to_knowledge():
    assert detect_intent_under("办理二类账户需要哪些材料", {}) == "knowledge"


# ── 边界：重演不能变成「什么都算缺陷」 ────────────────────────────────────────

def test_a_normal_business_question_is_not_a_reproduction():
    """正常业务问句在两边都不该拒答 —— 否则「复现」这个词就贬值了。"""
    proof = verify_bug_reproduction("办理二类账户需要哪些材料")

    assert proof["reproduced_before"] is False
    assert proof["is_regression_test"] is False
