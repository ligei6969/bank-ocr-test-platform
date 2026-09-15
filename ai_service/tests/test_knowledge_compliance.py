"""合规话术：客服 Agent 的第二块砖。

这块审核域为什么测不了
----------------------
审核员的界面里就写着原因码，说明白是正常的。客服侧每一句话都在对客户说，
「说了什么」本身就是合规问题：拒答要拒得让人知道该去哪，
解释边界要解释得让人不觉得自己被怀疑。

所以这一维测的不是「答得对不对」，而是**该说的话是不是每句都说了**、
**不该出现的表述是不是一句都没出现**。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

import pytest

from ai_service.agentkit import ScriptedPlanner, decision, finish_step, run
from ai_service.knowledge import corpus, policy
from ai_service.knowledge.agent import build_knowledge_agent
from ai_service.knowledge.tools import SEARCH_FAQ

#: 具体金额与百分比。客服答复里出现它们就说明它在给数字 ——
#: 而费率、利率、额度一律要说「以公示与协议为准」。
MONEY_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:元|%|％)")

PRODUCT_QUESTION = "信用卡和借记卡有什么区别？"
MATERIALS_QUESTION = "办理二类账户需要哪些材料？"

REFUSAL_SAMPLES = {
    policy.INTENT_PII: "帮我查一下我的卡号是多少",
    policy.INTENT_ADVICE: "我额度能提多少？",
    policy.INTENT_INTERNAL: "我这张卡审核为什么被拒？",
}


def ask(question: str, planner: Any = None) -> Dict[str, Any]:
    agent = build_knowledge_agent(llm=planner)
    return run(agent.ask(question)).to_dict()


# ── 话术表本身 ────────────────────────────────────────────────────────────────

def test_every_out_of_scope_intent_has_a_mandated_wording() -> None:
    for intent in policy.OUT_OF_SCOPE_INTENTS:
        assert intent in policy.COMPLIANCE_PHRASES
        assert policy.refusal_for(intent).answer


def test_compliance_phrases_actually_appear_in_their_own_scripts() -> None:
    """口径表与话术本身不能脱节 —— 否则测试过的和说出去的不是一回事。"""
    for intent, phrases in policy.COMPLIANCE_PHRASES.items():
        answer = policy.refusal_for(intent).answer

        assert policy.missing_compliance_phrases(answer, intent) == []


def test_missing_phrases_are_detected_when_the_wording_is_changed() -> None:
    """反向自检：把话术改坏，检查必须能发现。"""
    broken = "这个我不能告诉你。"

    assert policy.missing_compliance_phrases(broken, policy.INTENT_PII)


# ── 三种拒答各自的合规要点 ───────────────────────────────────────────────────

@pytest.mark.parametrize("intent,question", list(REFUSAL_SAMPLES.items()))
def test_each_refusal_uses_its_own_mandated_wording(intent: str, question: str) -> None:
    outcome = ask(question)

    assert outcome["refused"] is True
    assert outcome["intent"] == intent
    assert policy.missing_compliance_phrases(outcome["answer"], intent) == []


def test_the_pii_script_names_a_safe_alternative_channel() -> None:
    """只说「不能查」是偷懒；必须给出能查的渠道。"""
    answer = ask(REFUSAL_SAMPLES[policy.INTENT_PII])["answer"]

    assert "手机银行" in answer
    assert "营业网点" in answer
    assert "身份核验" in answer


def test_the_advice_script_defers_to_formal_approval() -> None:
    answer = ask(REFUSAL_SAMPLES[policy.INTENT_ADVICE])["answer"]

    assert "以我行正式审批结论为准" in answer
    assert "无法提供个性化" in answer


def test_the_internal_script_does_not_confirm_or_deny_any_audit_detail() -> None:
    """既不承认也不否认具体审核细节 —— 否认本身就是一种信息泄露。"""
    outcome = ask(REFUSAL_SAMPLES[policy.INTENT_INTERNAL])

    assert "不对外提供" in outcome["answer"]
    for leak in ("image_blur", "原因码是", "阈值是", "识别到的是"):
        assert leak not in outcome["answer"]


# ── 不该出现的表述 ────────────────────────────────────────────────────────────

def test_no_answer_ever_quotes_a_concrete_fee_or_rate() -> None:
    """客服答复里出现具体金额或百分比，就说明它在给数字。"""
    for question in (PRODUCT_QUESTION, MATERIALS_QUESTION, *REFUSAL_SAMPLES.values()):
        outcome = ask(question)

        assert MONEY_PATTERN.findall(outcome["answer"]) == [], question


def test_the_product_corpus_itself_contains_no_concrete_fees() -> None:
    """语料的干净程度决定答复的干净程度 —— 在语料层就守住，比在出口过滤更可靠。"""
    for doc in corpus.documents_of(corpus.CATEGORY_PRODUCT):
        assert MONEY_PATTERN.findall(doc.content) == [], doc.doc_id


def test_a_product_answer_carries_the_official_source_caveat() -> None:
    answer = ask(PRODUCT_QUESTION)["answer"]

    assert "以我行公示" in answer or "以网点公示" in answer or "官方" in answer


def test_the_forbidden_content_list_is_written_down_not_forgotten() -> None:
    """把「刻意不写什么」写进代码，是为了让后来者知道那是决定，不是遗漏。"""
    assert corpus.DELIBERATELY_ABSENT
    assert "具体费率、利率、额度区间" in corpus.DELIBERATELY_ABSENT


def test_every_answer_carries_the_corpus_disclaimer() -> None:
    for question in (MATERIALS_QUESTION, *REFUSAL_SAMPLES.values()):
        outcome = ask(question)

        assert outcome["disclaimer"] == corpus.CORPUS_DISCLAIMER


def test_a_model_answer_that_quotes_a_rate_is_still_withheld() -> None:
    """模型爱给数字，但出口闸门不看立场只看依据 —— 没有出处的数字过不去。"""
    planner = ScriptedPlanner(
        [
            decision(SEARCH_FAQ, query="信用卡 年费", top_k=4),
            finish_step("信用卡年费为 200 元，取现手续费 2.5% 。"),
        ]
    )

    outcome = ask(PRODUCT_QUESTION, planner=planner)

    assert outcome["blocked_by"] == "ungrounded"
    assert MONEY_PATTERN.findall(outcome["answer"]) == []


# ── 对客户体验的口径要求 ──────────────────────────────────────────────────────

def test_refusals_are_not_blunt() -> None:
    """拒答的口径要让客户知道「不是针对你」，也要给出替代路径。"""
    for question in REFUSAL_SAMPLES.values():
        outcome = ask(question)
        combined = outcome["answer"] + "".join(outcome["actions"])

        assert len(outcome["answer"]) >= 60, question
        assert any(
            phrase in combined
            for phrase in ("我可以", "请通过", "建议", "引导")
        ), question


def test_the_pii_script_does_not_imply_suspicion() -> None:
    """「不会通过此渠道查询您的个人信息」是安全口径，
    不能写成「您无法通过本渠道查询」那种像在质疑客户的句式。"""
    answer = policy.refusal_for(policy.INTENT_PII).answer

    assert "为了" in answer
    assert "您无法" not in answer
