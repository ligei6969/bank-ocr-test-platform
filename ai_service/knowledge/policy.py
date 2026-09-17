"""客服 Agent 的边界策略：意图判定、拒答话术、入参脱敏、接地审计。

为什么单独成模块
----------------
这四件事都是**确定性规则**，不该混进 Agent 的循环里。分出来的好处有三个：

1. 它们能被单独测（不需要构造 Agent、不需要假模型）；
2. 它们**优先于模型**：越界判定与拒答话术是代码，不是 prompt 里的一句请求；
3. 语义集中 —— 「什么算越界」「拒答要说什么」是产品与合规问题，
   不该散落在循环逻辑里，让人找不到。

为什么越界不能只靠 prompt
--------------------------
审核 Agent 踩过一次：把「证据不足必须转人工」只写在降级路径里，
结果模型（或被 prompt injection 影响的模型）回一个 ``finish`` 就绕过去了。
规则写在 prompt 里是**软约束** —— 模型可以选择不听，而且它往往听不出你在保护什么。

所以这里的立场是：**prompt 里也写，但真正的闸门在代码里。**
模型给越界问题编了一段像模像样的答复也没关系 —— 出口前会被
:func:`refusal_for` 的结论顶掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence, Tuple

# ── 意图分类 ──────────────────────────────────────────────────────────────────

#: 业务知识问题（本 surface 的服务范围）
INTENT_KNOWLEDGE = "knowledge"
#: 索要个人信息 / 系统内数据（越界）
INTENT_PII = "pii"
#: 索要个性化金融建议（越界）
INTENT_ADVICE = "advice"
#: 索要审核内部信息（越界）
INTENT_INTERNAL = "internal"
#: 查询具体产品（在范围内，但只能给通用信息，不能给个性化结论）
INTENT_PRODUCT = "product"

OUT_OF_SCOPE_INTENTS: frozenset[str] = frozenset(
    {INTENT_PII, INTENT_ADVICE, INTENT_INTERNAL}
)

#: 规则表。**顺序即优先级** —— 同时命中多条时以先命中的为准。
#:
#: 顺序是有讲究的：先判「索要数据/内部信息/建议」这类**动作性越界**，
#: 再判产品查询这种**范围之内的**意图。反过来会出现
#: 「我这张卡审核为什么被拒，额度能提多少」被判成普通产品咨询。
#:
#: 关键词的取舍原则：**优先拒答。** 一个模糊的问句被误判成越界，
#: 客户会收到一句合规口径 + 引导；被误判成范围内，渠道可能就答了不该答的。
#: 两种错误的代价不对称，所以宁可紧一点 —— 这也是为什么像「流水」这种
#: 可能出现在正常问句里的词仍然收进来。
_INTENT_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        INTENT_INTERNAL,
        (
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
            "识别出的原文",
            "识别出来的原文",
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
    ),
    (
        INTENT_PII,
        (
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
            "我的资料",
        ),
    ),
    (
        INTENT_ADVICE,
        (
            # 「我的额度是多少」这一族是 P4 冒烟时补上的。原来只有「额度能提」
            # 这类**提额**措辞，于是「我的额度是多少」被判成普通咨询 ——
            # 而口径是明确的：额度、利率一律拒答 + 引导（见 docs/下一步开发计划.md 第 2 条）。
            # 漏判的代价在这里特别大：它不是答错，是答了不该答的那一类。
            #
            # 为什么只加「我的 X」与「X 是多少 / X 多少」，不加裸词「额度」：
            # 账户侧自己的限额话题用的是「限额」（语料里也是这个词），
            # 「二类账户的限额是多少」是一句正常业务问题，裸词会把它一起拒掉。
            # 而信用卡的「额度」在语料里的正式答复本来就是
            # 「授信额度与审批结果以我行正式审批结论为准」—— 拒答与语料一致。
            "我的额度",
            "额度是多少",
            "额度多少",
            "我能有多少额度",
            "我的利率",
            "利率是多少",
            "利率多少",
            "我的授信",
            "我的利息",
            "额度能提",
            "能提多少",
            "能提额",
            "提额",
            "利率是多少我能",
            "利率最低",
            "给我多少",
            "我能贷",
            "能贷多少",
            "能批",
            "能下卡",
            "会通过吗",
            "我该办哪张",
            "我适合办",
            "该不该办",
            "值不值得办",
            "推荐我办",
            "买哪只",
            "理财推荐",
            "帮我选",
            "能不能批",
            "能过吗",
        ),
    ),
    (
        INTENT_PRODUCT,
        (
            "年费",
            "免息期",
            "积分",
            "权益",
            "产品参数",
            "这张卡有什么",
            "借记卡和信用卡",
            "有什么区别",
        ),
    ),
)

# ── 拒答话术 ──────────────────────────────────────────────────────────────────

#: 合规话术里必须出现的关键短语。测试按意图逐条校验 ——
#: 这是「合规话术」这一测试维度的判据，写在代码里而不是测试里，
#: 因为它同时是产品口径（换话术要改产品决定，不只是改测试）。
COMPLIANCE_PHRASES: Mapping[str, Tuple[str, ...]] = {
    INTENT_PII: ("不会通过此渠道查询", "请通过手机银行或营业网点"),
    INTENT_ADVICE: ("无法提供个性化", "以我行正式审批结论为准"),
    INTENT_INTERNAL: ("属于内部审核口径", "不对外提供"),
}


@dataclass(frozen=True)
class RefusalScript:
    """一次拒答的完整口径：说什么、下一步做什么、为什么。"""

    intent: str
    answer: str
    actions: Tuple[str, ...] = ()
    handoff_reason: str = ""
    missing_evidence: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "answer": self.answer,
            "actions": list(self.actions),
            "handoff_reason": self.handoff_reason,
            "missing_evidence": list(self.missing_evidence),
        }


_PII_SCRIPT = RefusalScript(
    intent=INTENT_PII,
    answer=(
        "为了保护您的账户与个人信息安全，我们**不会通过此渠道查询**您的卡号、"
        "身份证号、余额或交易明细，也不会受理任何「帮我查一下」的请求。"
        "如果您需要查询本人名下信息，请通过手机银行或营业网点办理，"
        "这些渠道会完成必要的身份核验。我可以帮您解答业务办理规则、"
        "所需材料与办理流程方面的问题。"
    ),
    actions=("引导用户通过手机银行或营业网点自助查询，不在本渠道处理任何账户数据检索。",),
    handoff_reason="用户请求涉及个人账户数据的查询，超出本渠道的服务范围",
    missing_evidence=("本人身份核验",),
)

_ADVICE_SCRIPT = RefusalScript(
    intent=INTENT_ADVICE,
    answer=(
        "很抱歉，授信额度、审批结果、利率优惠和产品选择这类问题，"
        "需要结合您的资信状况由我行审批系统判断，我**无法提供个性化**"
        "的金融建议，也不能预测审批结论 —— 任何具体数值都**以我行正式审批结论为准**。"
        "我可以帮您说明办理条件、所需材料、常见流程以及产品之间的通用差异，"
        "供您判断时参考。"
    ),
    actions=("告知可咨询的通用信息范围，引导用户到营业网点或官方客服获取正式结论。",),
    handoff_reason="用户请求个性化授信或投资建议，不属于本渠道可作答范围",
    missing_evidence=("用户资信状况", "正式审批规则"),
)

_INTERNAL_SCRIPT = RefusalScript(
    intent=INTENT_INTERNAL,
    answer=(
        "审核原因码、识别原文、内部阈值与风控规则**属于内部审核口径**，"
        "**不对外提供**，也不能通过本渠道查询或推测。"
        "如果您想了解的是「材料怎么准备、流程怎么走、被退件后如何补正」这类"
        "业务问题，我可以详细解答。"
    ),
    actions=(
        "告知内部审核信息不对外提供；如客户关心的是材料准备或流程问题，"
        "引导到营业网点或官方客服继续咨询。",
    ),
    handoff_reason="用户请求审核内部信息，属于明确的对外禁答范围",
    missing_evidence=("对外的审核说明口径",),
)

_SCRIPTS: Mapping[str, RefusalScript] = {
    INTENT_PII: _PII_SCRIPT,
    INTENT_ADVICE: _ADVICE_SCRIPT,
    INTENT_INTERNAL: _INTERNAL_SCRIPT,
}

#: 兜底：答不出时的统一口径。**「不知道」也是一种正确答案。**
INSUFFICIENT_ANSWER = (
    "我暂时无法确认这个问题的准确答案。为了保证信息可靠，"
    "我不会凭印象作答。建议您通过营业网点或我行官方客服核实，"
    "我可以协助把问题整理清楚，方便您一次问到点上。"
)


def detect_intent(question: str) -> str:
    """判定问题意图。纯规则、确定性、可单测。

    刻意不做「相似度 + 模型投票」那一套：越界判定是安全闸门，
    需要的是**可解释、可复现、改一行就改行为**。一个概率性的闸门
    在面试里也说不清 —— 你无法回答「它什么时候会漏」。
    """
    text = _normalize(question)
    for intent, keywords in _INTENT_RULES:
        if any(keyword in text for keyword in keywords):
            return intent
    return INTENT_KNOWLEDGE


def is_out_of_scope(intent: str) -> bool:
    return intent in OUT_OF_SCOPE_INTENTS


def refusal_for(intent: str) -> RefusalScript:
    """取越界意图的拒答口径。

    ``knowledge`` / ``product`` 调用本函数会**抛错**而不是返回一个「不需要拒答」
    的结果 —— 把「本来就不该问」变成显式失败，比返回一个空脚本
    然后被调用方忽略要安全得多。
    """
    script = _SCRIPTS.get(intent)
    if script is None:
        raise KeyError(f"意图 {intent!r} 不需要拒答，不应调用 refusal_for")
    return script


def missing_compliance_phrases(answer: str, intent: str) -> List[str]:
    """答复里缺失的合规关键短语。空列表 = 合规。"""
    required = COMPLIANCE_PHRASES.get(intent)
    if not required:
        return []
    return [phrase for phrase in required if phrase not in answer]


# ── 话题相关性 ────────────────────────────────────────────────────────────────

#: 词面覆盖率下限。低于它的召回结果**不算证据**。
#:
#: 为什么不能用检索分数
#: --------------------
#: 实测发现本服务的检索分数是**相对归一化**的：每次查询的最高分都被拉到 1.000，
#: 所以「今天天气怎么样」也能拿到满分命中。用分数做阈值等于什么也没挡。
#:
#: 改用「问题的二字词有多少能在语料里找到」这个绝对量。实测分离度：
#:
#: ========================  ==========================
#: 在范围内的问题             覆盖率 0.36 ~ 1.00
#: 越界问题（天气/股票/私人飞机） 覆盖率 0.00 ~ 0.17
#: ========================  ==========================
#:
#: 0.25 落在两簇之间的空档里。**这个数是实测标定的，不是拍的** ——
#: 但它仍然是启发式：语料换了、语言换了都要重新标定。
MIN_TERM_COVERAGE = 0.25


def bigrams(text: str) -> frozenset[str]:
    """取文本的二字词集合。单字太宽（「的」「是」遍地都是），不作为判据。"""
    from ai_service.retrieval import tokenize

    return frozenset(token for token in tokenize(text) if len(token) == 2)


def term_coverage(question: str, texts: Sequence[str]) -> float:
    """问题的二字词有多大比例出现在给定文本里。

    返回 0~1。没有二字词（极短问题、纯符号）时返回 0 —— 宁可不作答，
    也不要拿一个「无法判断」当「相关」。
    """
    wanted = bigrams(question)
    if not wanted:
        return 0.0
    haystack = "".join(texts)
    return sum(1 for token in wanted if token in haystack) / len(wanted)


# ── 入参脱敏 ──────────────────────────────────────────────────────────────────

#: 客服会收到用户随手粘贴的敏感信息（「帮我看看 6222 0202 0202 0001 这张卡」）。
#: 这些内容不该进检索、更不该进模型 prompt，因此在**入口**就抹掉。
_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    # 18 位身份证（含末位 X）
    ("身份证号", re.compile(r"\b\d{17}[\dXx]\b")),
    # 16~19 位连续或带分隔符的卡号
    ("银行卡号", re.compile(r"\b(?:\d[ -]?){15,19}\b")),
    # 手机号
    ("手机号", re.compile(r"\b1[3-9]\d{9}\b")),
)


def sanitize_question(text: str) -> Tuple[str, List[str]]:
    """抹掉问题里的敏感数字。返回 ``(脱敏文本, 命中的类别)``。

    保留「命中过什么」是为了可观测：入口静默改写用户输入，
    出问题时要有据可查，否则排查会变成猜。
    """
    if not text:
        return "", []

    masked = text
    hit: List[str] = []
    for label, pattern in _PATTERNS:
        if pattern.search(masked):
            hit.append(label)
            masked = pattern.sub(f"[已脱敏{label}]", masked)
    return masked, hit


# ── 接地审计 ──────────────────────────────────────────────────────────────────

#: 只看「像阈值的数字」：带小数点，或两位以上。
#: 单个个位数字大多是在数数（「三条材料」「第一步」），把它们算成编造会淹没真问题。
_THRESHOLD_NUMBER = re.compile(r"\d+\.\d+|\d{2,}")

#: 答复里出现这些词就说明它在给数字/结论，必须能在引用里找到依据。
_CLAIM_MARKERS: Tuple[str, ...] = (
    "元",
    "%",
    "利率",
    "额度",
    "上限",
    "下限",
    "不超过",
    "不少于",
    "至少",
    "以内",
    "大于",
    "小于",
)


@dataclass
class GroundingReport:
    """接地的体检结果。

    ``uncovered`` 非空代表答复里有**找不到出处的事实性内容** ——
    这就是幻觉。它是「接地 / 幻觉」这条测试维度的判据。
    """

    checked_numbers: int = 0
    uncovered: List[str] = field(default_factory=list)
    citations: int = 0

    @property
    def grounded(self) -> bool:
        return not self.uncovered

    @property
    def coverage(self) -> float:
        if self.checked_numbers == 0:
            return 1.0
        return (self.checked_numbers - len(self.uncovered)) / self.checked_numbers

    def as_dict(self) -> Dict[str, Any]:
        return {
            "grounded": self.grounded,
            "coverage": round(self.coverage, 4),
            "checked_numbers": self.checked_numbers,
            "uncovered": list(self.uncovered),
            "citations": self.citations,
        }


def audit_grounding(
    answer: str,
    citations: Sequence[Mapping[str, Any]],
) -> GroundingReport:
    """核对答复里的数字/结论是否都能在引用里找到出处。

    这是**审计**而不是拦截：模板答复天然接地，模型答复才需要核对。
    把它放进响应体，让「这段解释有没有出处」变成可观测事实，
    而不是靠人读一遍感觉「看起来挺靠谱」。
    """
    haystack = " ".join(
        str(item.get(key, ""))
        for item in citations
        if isinstance(item, Mapping)
        for key in ("title", "content", "snippet")
    )
    numbers = _THRESHOLD_NUMBER.findall(answer or "")
    uncovered = [
        number
        for number in numbers
        if number not in haystack and _looks_like_a_claim(answer, number)
    ]
    return GroundingReport(
        checked_numbers=len(numbers),
        uncovered=uncovered,
        citations=len(citations),
    )


def _looks_like_a_claim(answer: str, number: str) -> bool:
    """这个数字是不是「在陈述事实」而不是在数数。

    判据：数字附近出现单位或比较词。宁可漏判也不要误判 ——
    把「三条材料」判成幻觉，会让接地报告立刻失去可信度。
    """
    position = answer.find(number)
    window = answer[max(0, position - 12) : position + len(number) + 12]
    return any(marker in window for marker in _CLAIM_MARKERS)


def _normalize(text: str) -> str:
    """归一化：去空白、转小写、合并全角空格。

    关键词表里同时写了中英混排与全角写法，所以这里只做最保守的归一化，
    不做繁简转换之类的「聪明」处理 —— 转换规则一旦出错，
    失败方式是「漏判越界」，代价太高。
    """
    return re.sub(r"\s+", "", (text or "").lower())


__all__ = (
    "COMPLIANCE_PHRASES",
    "GroundingReport",
    "INSUFFICIENT_ANSWER",
    "INTENT_ADVICE",
    "INTENT_INTERNAL",
    "INTENT_KNOWLEDGE",
    "INTENT_PII",
    "INTENT_PRODUCT",
    "MIN_TERM_COVERAGE",
    "OUT_OF_SCOPE_INTENTS",
    "RefusalScript",
    "audit_grounding",
    "bigrams",
    "detect_intent",
    "is_out_of_scope",
    "missing_compliance_phrases",
    "refusal_for",
    "sanitize_question",
    "term_coverage",
)
