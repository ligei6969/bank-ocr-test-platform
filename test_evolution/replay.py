"""历史重演：在一个**不再复现该缺陷**的系统上，把旧行为执行出来。

为什么需要这个
--------------
CTE-1 的验收案例是「我征信上有什么问题」被判成普通咨询并作答 —— 一次真实的
漏拒。但这次漏拒**已经修好了**（``policy.py:100`` 加了「征信」关键词，
commit ``bb7947e``）。也就是说在当前 HEAD 上执行这个事件，只会得到
「正确拒答」，看不到当初的失败。

这不代表 CTE 做不了这件事 —— 恰恰相反，CTE 本来就该能回放历史失败。
但必须**显式声明测的是哪个版本**，否则 retro 里那句「复现了漏拒」
就是一句无法证伪的断言。

三种可选机制，以及为什么选第三种
--------------------------------
1. ``git worktree`` checkout 修复前的 commit —— 最真实，但要求工作区干净、
   依赖不变，且旧 commit 未必能跑起来（本项目的 ``policy.py`` 依赖一直在动）。
   作为自动化验证太脆。
2. 把旧规则表整个复制一份进仓库 —— 会形成「两套事实来源」，
   三个月后没人知道哪份是权威。
3. **预修复规则表作为 fixture 注入** —— 把修复前的关键词表以数据形式固化在
   :data:`POLICY_HISTORY` 里，执行时喂给评测函数而不是全局的 ``_INTENT_RULES``。

选 3。它快、确定、不依赖 git 状态，而且**如实记录了「当时系统长什么样」**：
fixture 里存的就是那份过窄的关键词表，任何人 can diff fixtur 与当前规则，
直接看出修复到底加了什么。代价是它只回放了**规则层**的行为，
不包含当时 prompt / 模型版本的差异 —— 这个限制写在
:class:`ReplayResult` 的 ``fidelity`` 字段里，不假装它是完整的历史重演。
"""

from __future__ import annotations

import re
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ai_service.knowledge import policy

#: 修复前的规则快照（``bb7947e`` 之前）。
#:
#: 只保留**与本案例相关的部分**，不是整份规则表的复制 —— 完整复制会变成
#: 第二套事实来源。这里存的是「当时 pii 一族的完整关键词」，够用来复现
#: 「去掉『我的』就绕过」这个具体缺陷，也不至于大得没人看。
POLICY_HISTORY: Dict[str, Dict[str, Any]] = {
    "policy@pre-bb7947e": {
        "description": "征信关键词只有「我的征信」，去掉前缀即绕过",
        "commit": "bb7947e^",
        "fixed_by": "bb7947e",
        "intents": {
            # 当时 pii 一族的完整表。注意这里**没有**裸词「征信」。
            policy.INTENT_PII: (
                "我的身份证号",
                "我的卡号",
                "我的手机号",
                "我的银行卡号",
                "帮我查",
                "查一下我的",
                "查我的",
                "我的余额",
                "我的流水",
                "交易流水",
                "流水",
                "我的交易",
                "我的征信",
                # ← 缺陷就在这里：「征信」不在表里
                "信用报告",
                "我的资料",
            ),
            policy.INTENT_ADVICE: (
                "我的额度",
                "额度是多少",
                "额度多少",
                "我能有多少额度",
                "我的利率",
                "利率是多少",
                "利率多少",
                "我的授信",
                "我的利息",
            ),
            policy.INTENT_INTERNAL: (
                "审核为什么",
                "为什么被拒",
                "为什么审核",
                "审核原因",
                "原因码",
                "ocr 原文",
                "ocr原文",
                "识别出的原文",
                "识别原文",
                "原始文本",
                "原始图片",
                "原文",
                "内部阈值",
                "阈值是多少",
                "风控规则",
                "模型怎么判",
                "模型是怎么判",
                "怎么判我",
                "怎么判",
                "你们系统怎么判",
            ),
        },
    },
}

# Captured before the audit repair. Kept as data and labelled rule-only;
# never consulted by the production classifier.
POLICY_HISTORY["policy@pre-EVT007"] = json.loads(
    (Path(__file__).parent / "snapshots/policy_pre_evt007.json").read_text(encoding="utf-8")
)


@dataclass
class ReplayResult:
    """一次历史重演的结果。"""

    system_version: str
    question: str
    intent: str
    out_of_scope: bool
    fidelity: str
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "system_version": self.system_version,
            "question": self.question,
            "intent": self.intent,
            "out_of_scope": self.out_of_scope,
            "fidelity": self.fidelity,
            "notes": list(self.notes),
        }


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def detect_intent_under(
    question: str, rules: Mapping[str, Sequence[str]]
) -> str:
    """用**给定的**规则表判定意图，而不是全局的 ``_INTENT_RULES``。

    顺序即优先级，与 ``policy.detect_intent`` 一致：先判动作性越界
    （internal / pii / advice），再落到 knowledge。
    """
    text = _normalize(question)
    for intent in (policy.INTENT_INTERNAL, policy.INTENT_PII, policy.INTENT_ADVICE, policy.INTENT_PRODUCT):
        keywords = rules.get(intent, ())
        if any(keyword in text for keyword in keywords):
            return intent
    return policy.INTENT_KNOWLEDGE


def replay(
    question: str, system_version: str = "policy@pre-bb7947e"
) -> ReplayResult:
    """在指定的历史规则版本上执行一个问题，返回当时的行为。

    ``system_version`` 不认识时抛 ``KeyError`` —— 与其静默回退到当前规则
    （那样 retro 会记录一个假的历史行为），不如直接失败。
    """
    snapshot = POLICY_HISTORY.get(system_version)
    if snapshot is None:
        raise KeyError(
            f"没有 {system_version!r} 的历史快照；"
            f"现有：{sorted(POLICY_HISTORY)}"
        )

    intent = detect_intent_under(question, snapshot["intents"])
    return ReplayResult(
        system_version=system_version,
        question=question,
        intent=intent,
        out_of_scope=policy.is_out_of_scope(intent),
        fidelity="rule_layer_only",
        notes=[
            f"快照取自 {snapshot['commit']}，由 {snapshot['fixed_by']} 修复",
            "只回放规则层：不含当时 prompt / 模型版本的差异。",
        ],
    )


def current_behaviour(question: str) -> ReplayResult:
    """当前 HEAD 上的行为，作为重演的对照。"""
    intent = policy.detect_intent(question)
    return ReplayResult(
        system_version="HEAD",
        question=question,
        intent=intent,
        out_of_scope=policy.is_out_of_scope(intent),
        fidelity="rule_layer_only",
    )


def verify_bug_reproduction(
    question: str,
    *,
    system_version: str = "policy@pre-bb7947e",
) -> Dict[str, Any]:
    """认证一个缺陷确实「修复前复现、修复后消失」。

    这是 ``NEW_TEST`` 类型 Candidate 的 ``reproduces_before_fix`` 检查的判据，
    也正是方案第十三节要求的：一个测试的价值不在于「pytest pass」，
    而在于**它在修复前会 FAIL、修复后会 PASS**。永远 ``assert True``
    的测试同样 pass，但那种东西不是资产。

    返回 ``{"reproduced_before": bool, "fixed_after": bool, "is_regression_test": bool}``。
    ``is_regression_test`` 两者都真时才算 —— 只有一个真说明这条用例
    要么抓不到旧缺陷（抓不住回归），要么在当前版本上仍然失败（是未修的 bug）。
    """
    before = replay(question, system_version)
    after = current_behaviour(question)

    # 「复现」的定义是：**在该判定本应拒答的那个问题上**，旧版本漏拒了。
    # 不能只看 ``before.out_of_scope`` 是不是 False —— 一句正常业务问题
    # 在哪个版本都不拒答，那叫「问得对」，不叫「复现了缺陷」。
    # 判据取当前版本的行为作为「本应如何」：当前拒答、旧版本放行，
    # 才是货真价实的回归被抓住。
    should_refuse = after.out_of_scope
    reproduced_before = should_refuse and not before.out_of_scope
    fixed_after = should_refuse and after.out_of_scope
    return {
        "question": question,
        "system_version": system_version,
        "should_refuse": should_refuse,
        "reproduced_before": reproduced_before,
        "fixed_after": fixed_after,
        "is_regression_test": reproduced_before and fixed_after,
        "before": before.to_dict(),
        "after": after.to_dict(),
    }


__all__ = (
    "POLICY_HISTORY",
    "ReplayResult",
    "current_behaviour",
    "detect_intent_under",
    "replay",
    "verify_bug_reproduction",
)
