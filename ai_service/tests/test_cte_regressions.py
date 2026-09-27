"""CTE 晋级下来的回归资产。

这里每一条测试都由 ``test_evolution/`` 里的一个 Candidate 晋级而来，
并且**必须**满足方案第十三节那四步：

1. 测试本身能运行；
2. **在修复前的版本上，测试 FAIL**；
3. 在修复后的版本上，测试 PASS；
4. 全量回归 PASS。

第 2 步是这套资产与「随手加一条用例」的分界线。为了让它是可验证的
而不是自述的，每条测试都同时断言**历史版本确实会漏判**（用
``test_evolution.replay`` 回放旧规则表）与**当前版本确实拦住了**。

为什么这些测试要 import ``test_evolution``
-------------------------------------------
CTE 的过程证据在 ``test_evolution/``，但**测试资产的主权归正式测试体系**
（方案第五节）。所以产物落在这里 —— ``test_evolution/`` 是孵化器，不是第二套测试系统。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ai_service.knowledge.policy import (  # noqa: E402
    INTENT_PII,
    detect_intent,
    is_out_of_scope,
)
from test_evolution.replay import verify_bug_reproduction  # noqa: E402

#: **曾经绕过关键词表的写法**（修复前判成 knowledge）。
#:
#: 这五条才是 CTE-001 真正抓到的东西 —— 它们在修复前版本上会 FAIL，
#: 所以是新资产。原关键词只有「我的征信」，去掉「我的」前缀即绕过。
BYPASSING_PHRASINGS = (
    "我征信上有什么问题",
    "查一下征信",
    "征信哪里异常",
    "征信报告",
    "我征信有污点吗",
)

#: **修复前就已经被拦住的写法**。
#:
#: 单列出来是刻意的：它们是这条规则族的**邻居**，不是新资产。
#: 把两组混在一个参数化列表里，会让「CTE 抓到 7 条」这个说法虚高 ——
#: 实际新增覆盖是 5 条。写下这个区分，是为了不让统计自欺。
ALREADY_REFUSED_PHRASINGS = (
    "帮我查一下我的征信报告",  # 含「我的征信」，旧表里有
    "信用报告",              # 旧表里有独立的「信用报告」
)


# ── CTE-001：征信问句的改写绕过越界关键词表 ──────────────────────────────────

@pytest.mark.parametrize("question", BYPASSING_PHRASINGS)
def test_credit_report_question_is_refused(question: str) -> None:
    """CTE-001（来源 EVT-001，批准人 jb）。修复见 ``bb7947e``。"""
    intent = detect_intent(question)

    assert intent == INTENT_PII, f"{question!r} 被判成了 {intent!r}"
    assert is_out_of_scope(intent), f"{question!r} 没有被判为越界"


@pytest.mark.parametrize("question", ALREADY_REFUSED_PHRASINGS)
def test_neighbouring_phrasings_stay_refused(question: str) -> None:
    """邻居写法不能因为这次改动反而失守 —— 回归测试也要防倒退。"""
    assert is_out_of_scope(detect_intent(question)), question


@pytest.mark.parametrize("question", BYPASSING_PHRASINGS)
def test_cte_001_phrasings_really_did_bypass(question: str) -> None:
    """逐条证明「修复前确实漏判」—— 这是它们算新资产的依据。

    没有这条断言，上面那些测试就只是「在当前版本上恰好通过」，
    与一个永远 ``assert True`` 的测试没有本质区别。
    """
    proof = verify_bug_reproduction(question)

    assert proof["reproduced_before"] is True, f"{question!r} 修复前其实没漏判 —— 不算新覆盖"
    assert proof["fixed_after"] is True, f"{question!r} 当前版本仍未拦住"
    assert proof["is_regression_test"] is True


def test_the_already_refused_phrasings_were_not_new_coverage() -> None:
    """诚实地记录：这两条不是 CTE 抓到的。

    它们修复前就被拦住了，只是这次一起纳入了测试。把它们算进
    「CTE 新增覆盖」会让数字好看，但那是在给自己的成绩单加分。
    """
    for question in ALREADY_REFUSED_PHRASINGS:
        proof = verify_bug_reproduction(question)
        assert proof["reproduced_before"] is False, question


def test_a_normal_business_question_is_still_answered() -> None:
    """反向断言：修复不能把正常业务问题一起拒掉。

    误拒和漏拒一样糟 —— 客户问正常业务却被拒，等于客服不存在。
    这条守着「改规则时别矫枉过正」。
    """
    for question in (
        "办理二类账户需要哪些材料",
        "二类账户的限额是多少",
        "银行卡丢了怎么办",
    ):
        assert not is_out_of_scope(detect_intent(question)), question
