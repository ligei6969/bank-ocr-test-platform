"""接地 / 幻觉：客服 Agent 的第一块砖。

这块审核域为什么测不了
----------------------
审核 Agent 谈的是「这条记录为什么被拒」——事实来自平台传进来的记录与原因码语料，
本来就有一份权威依据。客服侧没有这条约束：客户问的是一个自由问题，
模型完全可以凭训练记忆答一段听起来对、实际上过期的业务规则。

**「听起来对」比「明显错」危险得多**，因为没有人会去核查一段流畅的回答。
所以这里的判据是硬性的：**答复里的每个数字都必须能在引用里找到出处**；
一条引用都没有却给了实质性答复的，直接判失败。
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from ai_service.agentkit import ScriptedPlanner, decision, finish_step, run
from ai_service.knowledge import policy
from ai_service.knowledge.agent import build_knowledge_agent
from ai_service.knowledge.tools import SEARCH_FAQ

QUESTION = "办理二类账户需要哪些材料？"

CITATIONS = [
    {
        "doc_id": "kb.faq.class2_account",
        "title": "二类账户开户需要哪些材料",
        "content": (
            "业务口径：二类账户可以办理存款、理财购买。"
            "注意事项：身份证件须在有效期内。"
            "单笔转入不超过 10000 元时无需额外核验。"
        ),
    }
]


def ask(question: str, planner: Any = None) -> Dict[str, Any]:
    agent = build_knowledge_agent(llm=planner)
    return run(agent.ask(question)).to_dict()


# ── 审计函数本身 ──────────────────────────────────────────────────────────────

def test_a_number_found_in_the_citations_is_grounded() -> None:
    report = policy.audit_grounding("单笔转入不超过 10000 元时无需核验。", CITATIONS)

    assert report.grounded is True
    assert report.checked_numbers == 1


def test_a_number_absent_from_the_citations_is_flagged() -> None:
    report = policy.audit_grounding("单笔转入不超过 50000 元时无需核验。", CITATIONS)

    assert report.grounded is False
    assert report.uncovered == ["50000"]


def test_a_fabricated_rate_is_caught() -> None:
    """编造费率是这一域最典型、也最容易被当成真的幻觉。"""
    report = policy.audit_grounding("我行账户管理费为 3.5% 每年。", CITATIONS)

    assert report.uncovered == ["3.5"]


def test_small_counts_are_not_treated_as_claims() -> None:
    """「三条材料」「第一步」在数数，不是事实主张。

    把它们算成幻觉会让接地报告立刻失去可信度 —— 误报几次之后没人再看它。
    """
    report = policy.audit_grounding("办理需要准备 3 条材料，第 1 步是填写申请书。", CITATIONS)

    assert report.uncovered == []
    assert report.grounded is True


def test_an_empty_answer_is_trivially_grounded() -> None:
    assert policy.audit_grounding("", []).grounded is True


def test_coverage_is_reported_for_visibility() -> None:
    report = policy.audit_grounding("费率是 3.5% ，上限 10000 元。", CITATIONS)

    assert report.checked_numbers == 2
    assert report.coverage == pytest.approx(0.5)


# ── Agent 出口闸门 ────────────────────────────────────────────────────────────

def test_the_offline_answer_is_grounded_by_construction() -> None:
    outcome = ask(QUESTION)

    assert outcome["grounding"]["grounded"] is True
    assert outcome["citations"]


def test_a_fabricated_number_from_the_model_is_blocked() -> None:
    """模型编了一个语料里没有的数字 —— 答复被顶掉，改走转人工。"""
    planner = ScriptedPlanner(
        [
            decision(SEARCH_FAQ, query="二类户 材料", top_k=4),
            finish_step("二类账户的管理费为 5.5% ，且需要提供 30000 元最低存款。"),
        ]
    )

    outcome = ask(QUESTION, planner=planner)

    assert outcome["blocked_by"] == "ungrounded"
    assert outcome["stop_reason"] == "ungrounded"
    assert "5.5" not in outcome["answer"]
    assert "30000" not in outcome["answer"]
    # 被拦下的原文要留档，否则无法判断闸门拦得对不对
    assert "5.5" in outcome["rejected_answer"]
    assert outcome["handoff"]["escalated"] is True


def test_an_answer_without_any_citation_is_blocked() -> None:
    """没查就答 = 从记忆里编。知识库是唯一事实来源，这条没有例外。"""
    planner = ScriptedPlanner(
        [finish_step("二类账户需要携带身份证到网点办理，当场可以办结。")]
    )

    outcome = ask(QUESTION, planner=planner)

    assert outcome["blocked_by"] == "ungrounded"
    assert "没有引用任何知识片段" in outcome["handoff"]["reason"]


def test_a_grounded_model_answer_is_kept_and_reaches_the_user() -> None:
    """闸门不能只拦不放过 —— 有出处的答复必须原样保留。"""
    planner = ScriptedPlanner(
        [
            decision(SEARCH_FAQ, query="二类户 材料 办理流程", top_k=4),
            finish_step("办理二类账户需要本人有效身份证件原件与实名登记的手机号，可到网点当场办结。"),
        ]
    )

    outcome = ask(QUESTION, planner=planner)

    assert outcome["blocked_by"] == ""
    assert outcome["answer"].startswith("办理二类账户需要")
    assert outcome["rejected_answer"] == ""


def test_a_refusal_is_not_measured_against_the_grounding_ruler() -> None:
    """拒答说的是合规口径，不是事实主张 —— 用「必须有引用」去要求它属于拿错尺子。"""
    outcome = ask("我额度能提多少？")

    assert outcome["refused"] is True
    assert outcome["blocked_by"] == ""
    assert outcome["grounding"]["grounded"] is True


def test_an_off_topic_question_ends_as_ungrounded_not_as_a_guess() -> None:
    outcome = ask("你们家的私人飞机怎么买？")

    assert outcome["stop_reason"] == "ungrounded"
    assert outcome["citations"] == []
    assert outcome["answer"] == policy.INSUFFICIENT_ANSWER
    assert outcome["handoff"]["escalated"] is True


def test_every_outcome_carries_a_grounding_report() -> None:
    for question in (QUESTION, "我额度能提多少？", "你们家的私人飞机怎么买？"):
        outcome = ask(question)

        assert set(outcome["grounding"]) == {
            "grounded",
            "coverage",
            "checked_numbers",
            "uncovered",
            "citations",
        }


def test_the_blocked_answer_is_kept_for_audit_but_never_shown() -> None:
    """留档与展示是两件事：留档给审计看，展示给客户看。"""
    planner = ScriptedPlanner(
        [
            decision(SEARCH_FAQ, query="二类户", top_k=4),
            finish_step("年费为 12.8% 。"),
        ]
    )

    outcome = ask(QUESTION, planner=planner)
    serialized = json.dumps(outcome, ensure_ascii=False)

    assert "12.8" in outcome["rejected_answer"]
    assert "12.8" not in outcome["answer"]
    # 明文出现在被拦原文里是可接受的（审计需要），但必须显式标记为被拦
    assert '"blocked_by": "ungrounded"' in serialized
