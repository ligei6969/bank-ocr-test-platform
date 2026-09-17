"""客服 Agent：planner + executor 的多步决策循环。

与审核 Agent 的关系
-------------------
循环的**机制**共用（:mod:`ai_service.loop` 的记账与截断、``tool_manager`` 的
白名单 / schema / 熔断 / 缓存 / 兜底、``structured`` 的解析、``cassette`` 的回放），
**策略**各写各的 —— 两个 surface 对「什么时候该举手」的答案完全不同，
硬抽成一个模板只会得到一堆 ``if surface == ...``，比重复更糟。

本模块新增的是三件事：

1. **出口闸门**：越界判定与拒答话术是确定性代码，模型编得再像也会被顶掉；
2. **接地闸门**：答复里有找不到出处的数字 / 结论，或压根没查就直接作答，
   一律退回「无法确认 + 转人工」；
3. **确定性兜底答案**：没有模型时，从命中语料直接拼出完整答复 ——
   事实层本来就不依赖模型，所以降级损失的是措辞，不是可用性。

第 1、2 条都是「规则优先于模型」的具体应用。审核 Agent 用一次真实缺陷
（模型一句 ``finish`` 就绕过转人工规则）证明过：写在流程里的规则容易被绕过，
**写在出口的闸门不会**。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

from ai_service.agent import AgentBudget, AgentDecision
from ai_service.explain import to_citation
from ai_service.knowledge import corpus, policy
from ai_service.knowledge.corpus import CORPUS_DISCLAIMER, CATEGORY_TITLES
from ai_service.knowledge.prompts import KNOWLEDGE_DECIDE, KNOWLEDGE_AGENT_PROMPT_ID
from ai_service.knowledge.session import DEFAULT_MAX_TURNS, SessionHistory, Turn
from ai_service.knowledge.tools import (
    HANDOFF_TO_HUMAN,
    KNOWLEDGE_TOOL_WHITELIST,
    LOOKUP_PRODUCT,
    SEARCH_FAQ,
    SEARCH_POLICY,
    build_knowledge_tools,
    faq_retriever,
    policy_retriever,
    tool_catalog_text,
)
from ai_service.llm import LLMClient, LLMUnavailableError, NullLLMClient
from ai_service.loop import TokenLedger
from ai_service.loop import clip as _clip
from ai_service.loop import summarize_observation
from ai_service.prompts import PROMPT_REGISTRY_VERSION, collect_labels
from ai_service.retrieval import KnowledgeRetriever
from ai_service.structured import StructuredOutputError, parse_structured
from ai_service.tool_manager import ToolManager

logger = logging.getLogger(__name__)

FINISH = "finish"

DEFAULT_MAX_STEPS = 5
DEFAULT_MAX_TOKENS = 3000
DEFAULT_MAX_TOOL_CALLS = 6
MAX_REJECTIONS = 3
MAX_IDENTICAL_CALLS = 2

THOUGHT_CHARS = 200
OBSERVATION_CHARS = 400
HISTORY_OBSERVATION_CHARS = 220

#: 答复超过这个长度就认为是「实质性作答」。用于判断「没查就答」：
#: 这么长的答复必然在陈述事实，没有引用就是幻觉。
#:
#: 20 是按实测定的：寒暄类答复（「您好，请问有什么可以帮您」）在 10~15 字，
#: 而定性的业务答复（「二类账户需携带身份证到网点办理」）在 20 字以上。
#: **宁可低一点**：漏判意味着一条无出处的答复被放行，那是这个闸门唯一
#: 不能接受的方向；误判只是多转一次人工。
SUBSTANTIVE_ANSWER_CHARS = 20

#: 兜底答复里最多引用几条语料。多了会变成粘贴整篇文档。
FALLBACK_CITATION_LIMIT = 3

#: 兜底答复里最多列几条事实性段落（材料 / 流程 / 注意事项）。
#: 客服答复是给人读的，不是把文档倒出来 —— 四条以上就该引导去官方渠道了。
MAX_FACT_LINES = 5

#: 相对相关性门槛：低于「最佳命中覆盖率 × 这个系数」的文档不进答复。
#: 绝对门槛只管「这句话题相不相关」，相对门槛管「这条比最佳那条差多少」。
RELEVANCE_RATIO = 0.6

#: 精确查表命中的覆盖率。关键词命中就是高置信，不打折（见 ``_absorb``）。
EXACT_MATCH_COVERAGE = 1.0

#: 兜底答复按这个顺序组织段落 —— 顺序就是客户关心的顺序
_SECTION_ORDER: tuple[str, ...] = (
    "业务口径",
    "所需材料",
    "办理流程",
    "注意事项",
    "常见退件",
)


# ── 预算 ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KnowledgeBudget(AgentBudget):
    """客服侧预算。步数比审核侧少：问题域更浅，不需要那么多轮探查。

    直接继承审核侧的 :class:`AgentBudget` —— 预算结构是**机制**，
    默认值才是策略。继承让「预算长什么样」只有一份定义。

    ⚠️ 必须重新加 ``@dataclass`` 装饰器：不加的话字段注解不会生成新的
    ``__init__``，实际生效的还是基类的默认值，看起来像「改了没生效」。
    """

    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS


# ── 产出 ──────────────────────────────────────────────────────────────────────

@dataclass
class KnowledgeOutcome:
    """客服答复。各条路径（模型 / 兜底 / 拒答）共用这一个结构。"""

    question: str
    intent: str = policy.INTENT_KNOWLEDGE
    answer: str = ""
    citations: List[Dict[str, Any]] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    handoff: Optional[Dict[str, Any]] = None
    refused: bool = False
    #: 被出口闸门拦下的答复正文。保留它不是为了展示，而是为了**可审计** ——
    #: 要能回答「模型当时想说什么」，否则无法判断闸门拦得对不对。
    rejected_answer: str = ""
    blocked_by: str = ""
    sanitized: List[str] = field(default_factory=list)
    grounding: Dict[str, Any] = field(default_factory=dict)
    #: 因话题不相关被丢掉的召回条数（「去偏」这件事的可观测证据）
    off_topic_dropped: int = 0
    degraded: bool = True
    truncated: bool = False
    stop_reason: str = "finished"
    decision_engine: str = "rule"
    budget: Dict[str, Any] = field(default_factory=dict)
    budget_used: Dict[str, Any] = field(default_factory=dict)
    token_usage: Dict[str, Any] = field(default_factory=dict)
    prompt_versions: Dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    trace: List[Dict[str, Any]] = field(default_factory=list)
    tools: Dict[str, Any] = field(default_factory=dict)
    retrieval_backend: str = ""
    llm: str = "none"
    llm_available: bool = False
    #: 更新后的多轮会话历史（含本轮）。调用方拿回去，下一轮随请求带进来。
    #:
    #: **服务端不留存**：这个字段是「回执」，不是「状态」。
    #: 它已经被裁剪到 :data:`~ai_service.knowledge.session.DEFAULT_MAX_TURNS` 轮 ——
    #: 传回去也没人会用的部分不该跟着响应一起长大。
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "intent": self.intent,
            "citations": self.citations,
            "actions": self.actions,
            "handoff": self.handoff,
            "refused": self.refused,
            "blocked_by": self.blocked_by,
            "rejected_answer": self.rejected_answer,
            "sanitized": self.sanitized,
            "grounding": self.grounding,
            "off_topic_dropped": self.off_topic_dropped,
            "degraded": self.degraded,
            "truncated": self.truncated,
            "stop_reason": self.stop_reason,
            "history": self.history,
            "engine": {
                "llm": self.llm,
                "llm_available": self.llm_available,
                "decision": self.decision_engine,
                "retrieval": self.retrieval_backend,
            },
            "budget": self.budget,
            "budget_used": self.budget_used,
            "token_usage": self.token_usage,
            "prompt_versions": self.prompt_versions,
            "latency_ms": self.latency_ms,
            "trace": self.trace,
            "tools": self.tools,
            "disclaimer": CORPUS_DISCLAIMER,
        }


@dataclass
class _AskState:
    """一轮问答的累计状态。"""

    question: str
    #: 本轮携带的会话历史（已脱敏、已裁剪）。**仅用于 prompt 里的指代理解** ——
    #: 它不进 ``evidence``，因此既不会变成引用，也不会让接地闸门放行无来源内容。
    session: SessionHistory = field(default_factory=SessionHistory)
    #: 历史块的渲染结果。在 ``ask()`` 里算一次就不再变 ——
    #: 本轮内部它必须恒定，否则「同一问题同一步」会因渲染时机不同而不同。
    session_block: str = ""
    intent: str = policy.INTENT_KNOWLEDGE
    step_index: int = 0
    tool_calls: int = 0
    rejections: int = 0
    ledger: TokenLedger = field(default_factory=TokenLedger)
    decision_engine: str = "llm"
    truncated: bool = False
    stop_reason: str = "finished"
    answer: str = ""
    actions: List[str] = field(default_factory=list)
    refused: bool = False
    rejected_answer: str = ""
    blocked_by: str = ""
    sanitized: List[str] = field(default_factory=list)
    grounding: Dict[str, Any] = field(default_factory=dict)
    prompt_label: Optional[str] = None
    trace: List[Dict[str, Any]] = field(default_factory=list)
    evidence: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    signatures: Dict[str, int] = field(default_factory=dict)
    handoff: Optional[Dict[str, Any]] = None
    #: 因为话题不相关被丢掉的召回条数。留痕是为了让「为什么答不出」有据可查
    off_topic_dropped: int = 0

    @property
    def citations(self) -> List[Dict[str, Any]]:
        """已取到的引用，按话题相关性排序。

        由 ``evidence`` **派生**而不是另存一份：两处状态一旦不同步，
        接地检查看到的引用集合与最终返回给前端的就可能不是同一批 ——
        那种 bug 会表现为「报告说接地了，但用户看到的引用里没有依据」。
        """
        return [to_citation(hit) for hit in rank_evidence(self.evidence)]


def rank_evidence(evidence: Mapping[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按话题相关性排序并过滤后的证据。

    只保留与最佳命中**同一量级**的文档：一条覆盖率高、其余明显更低时，
    低的那批基本是词面巧合（「有什么区别」这种问句到处都有）。
    留着它们会把答复变成「正确段落 + 无关段落」的混合物，
    而读者很难分辨哪段才是答案。

    相对门槛（``RELEVANCE_RATIO`` × 最佳）与绝对门槛（``MIN_TERM_COVERAGE``）
    都要过。做成模块级函数是因为**接地检查与最终输出必须用同一批引用** ——
    两处各算一遍，迟早会算出不一样的结果。
    """
    ranked = sorted(
        evidence.values(),
        key=lambda item: float(item.get("_coverage") or 0.0),
        reverse=True,
    )
    if not ranked:
        return []
    floor = max(
        policy.MIN_TERM_COVERAGE,
        float(ranked[0].get("_coverage") or 0.0) * RELEVANCE_RATIO,
    )
    return [item for item in ranked if float(item.get("_coverage") or 0.0) >= floor]


# ── Agent ─────────────────────────────────────────────────────────────────────

class KnowledgeAgent:
    """银行业务知识客服 Agent。"""

    def __init__(
        self,
        *,
        tools: ToolManager,
        llm: Optional[LLMClient] = None,
        budget: Optional[AgentBudget] = None,
        retrieval_backend: str = "",
    ) -> None:
        self._tools = tools
        self._llm: LLMClient = llm or NullLLMClient()
        self._budget = budget or KnowledgeBudget()
        self._retrieval_backend = retrieval_backend

    @property
    def tools(self) -> ToolManager:
        """暴露出来是为了让测试能替换某个工具的 handler（故障注入）。"""
        return self._tools

    @property
    def budget(self) -> AgentBudget:
        return self._budget

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def ask(
        self,
        question: str,
        history: Optional[SessionHistory] = None,
    ) -> KnowledgeOutcome:
        """回答一个问题。**任何情况下都不抛异常。**

        ``history`` 是调用方持有的多轮会话历史。``None``（默认）表示单轮 ——
        这条路径的行为与没有多轮功能时完全一致，是向后兼容的基线。

        安全立场（三条，都有对应测试）：

        1. **闸门只按当轮问题判定**：越界与否看的是 ``question`` 本身，
           历史里的任何内容都不能改变这个结论；
        2. **历史不产生事实**：历史不进证据集，因此不会变成引用、
           也不会让接地闸门把无来源的答复放行；
        3. **历史先洗再用**：进 prompt 前强制脱敏与裁剪，且
           ``refused`` / ``intent`` 从文本重算，不采信调用方传来的标志位。
        """
        started = time.monotonic()
        clean, sanitized = policy.sanitize_question(question)
        # 先洗、再裁、后渲染。顺序在这里就定死，不指望调用方先做对。
        session = (history or SessionHistory()).sanitize().trim(DEFAULT_MAX_TURNS)
        state = _AskState(
            question=clean or str(question or "").strip(),
            session=session,
            session_block=session.to_prompt_block(),
        )
        state.sanitized = sanitized
        state.intent = policy.detect_intent(state.question)

        if not state.question:
            state.stop_reason = "empty_question"
            state.answer = "请描述您想了解的业务问题，例如「办理二类账户需要哪些材料」。"
        elif policy.is_out_of_scope(state.intent):
            # 越界：不进循环，直接出口闸门。模型没有机会「尝试回答」——
            # 这是有意的：让它先答再拦，等于把越界内容生成了一次，
            # 而生成本身就会进日志、进 trace、进模型供应商的日志。
            await self._refuse(state)
        elif self._llm.available:
            await self._run_llm_loop(state)
        else:
            logger.info("LLM 不可用，客服 Agent 走确定性检索序列")
            await self._run_deterministic(state)

        await self._enforce_grounding(state)
        return self._finalize(state, started)

    # ── 出口闸门一：越界 ──────────────────────────────────────────────────────

    async def _refuse(self, state: _AskState) -> None:
        """按意图套用合规口径，并把「需要人工」显式化。

        话术来自 :mod:`ai_service.knowledge.policy`，**不是模型生成的**。
        模型可以参与措辞（后续版本），但口径本身必须固定 ——
        合规话术一旦允许自由发挥，就没法保证每一次都说了该说的话。
        """
        script = policy.refusal_for(state.intent)
        state.refused = True
        state.answer = script.answer
        state.actions = list(script.actions)
        state.decision_engine = "policy"

        # 先记账再改 stop_reason：``_execute`` 在转人工成功时会写
        # ``escalated``（那是它作为通用步骤该做的事），而拒答是更精确的终局原因，
        # 顺序反了就会被覆盖成 escalated，统计「拒答率」时全部漏掉。
        await self._execute(
            state,
            AgentDecision(
                thought="问题超出本渠道范围，按合规口径拒答并转人工",
                action=HANDOFF_TO_HUMAN,
                params={
                    "reason": script.handoff_reason,
                    "missing_evidence": list(script.missing_evidence),
                },
            ),
        )
        state.stop_reason = "refused"

    # ── 出口闸门二：接地 ──────────────────────────────────────────────────────

    async def _enforce_grounding(self, state: _AskState) -> None:
        """答复必须能落到语料上，否则退回「无法确认 + 转人工」。

        两种触发条件：

        1. **有编造**：答复里的数字 / 结论在引用里找不到出处；
        2. **没查就答**：一条引用都没有，却给出了一段实质性答复 ——
           那只能是从记忆里编的。知识库是唯一事实来源，这条没有例外。

        拒答路径不走这里：它说的是合规口径，不是事实主张，
        用「必须有引用」去要求它属于拿错尺子量。
        """
        if state.refused or not state.answer:
            state.grounding = policy.audit_grounding(state.answer, []).as_dict()
            return

        report = policy.audit_grounding(state.answer, state.citations)
        state.grounding = report.as_dict()

        ungrounded_reasons: List[str] = []
        if report.uncovered:
            ungrounded_reasons.append(
                f"答复中的 {len(report.uncovered)} 处数值/结论找不到出处"
            )
        if not state.citations and len(state.answer) >= SUBSTANTIVE_ANSWER_CHARS:
            ungrounded_reasons.append("答复没有引用任何知识片段")

        if not ungrounded_reasons:
            return

        logger.warning("客服答复未通过接地检查: %s", "；".join(ungrounded_reasons))
        state.rejected_answer = state.answer
        state.blocked_by = "ungrounded"
        state.answer = policy.INSUFFICIENT_ANSWER
        state.actions = ["该问题未能给出有出处的答复，已按流程转人工核实。"]
        await self._execute(
            state,
            AgentDecision(
                thought="答复缺少可核对的出处，改为转人工",
                action=HANDOFF_TO_HUMAN,
                params={
                    "reason": "；".join(ungrounded_reasons),
                    "missing_evidence": ["语料中可核对的依据"],
                },
            ),
            allow_when_exhausted=True,
        )
        # 与拒答同理：``_execute`` 把转人工的成功记成 ``escalated``，
        # 而闸门拦下的原因更精确，必须写在它之后，否则被覆盖。
        state.stop_reason = "ungrounded"

    # ── LLM 决策循环 ──────────────────────────────────────────────────────────

    async def _run_llm_loop(self, state: _AskState) -> None:
        for step_index in range(1, self._budget.max_steps + 1):
            state.step_index = step_index
            if state.tool_calls >= self._budget.max_tool_calls:
                state.stop_reason = "max_tool_calls"
                state.truncated = True
                return
            if state.ledger.tokens >= self._budget.max_tokens:
                state.stop_reason = "max_tokens"
                state.truncated = True
                return

            decision = await self._decide(state)
            if decision is None:
                # 模型抽风不该让整条链路失败：退回确定性路径继续，而不是报错
                state.decision_engine = "rule"
                await self._run_deterministic(state, resume=True)
                return

            if decision.action == FINISH:
                state.trace.append(
                    {
                        "step": state.step_index,
                        "kind": "finish",
                        "thought": _clip(decision.thought, THOUGHT_CHARS),
                        "prompt": state.prompt_label,
                    }
                )
                state.answer = decision.answer.strip() or self._fallback_answer(state)
                state.stop_reason = "finished"
                return

            if not await self._execute(state, decision):
                return

        state.stop_reason = "max_steps"
        state.truncated = True

    async def _decide(self, state: _AskState) -> Optional[AgentDecision]:
        """一次 planner 调用。失败返回 ``None``，由调用方决定怎么降级。"""
        prompt = KNOWLEDGE_DECIDE.render(
            question=state.question,
            # 会话历史与「已完成的步骤」是两个不同的东西，模板里也是两个占位符：
            # 前者是跨轮次的对话，后者是本轮已经查过什么。混在一起会让模型
            # 分不清「上一轮用户说的」和「这一轮工具返回的」。
            session=state.session_block,
            history=self._history_block(state),
            tool_catalog=tool_catalog_text(),
        )
        try:
            raw = await self._llm.complete(
                prompt,
                system=KNOWLEDGE_DECIDE.system,
                max_tokens=512,
                temperature=0.0,
            )
        except LLMUnavailableError as exc:
            logger.warning("客服 Agent 决策调用失败，转为确定性序列: %s", exc)
            state.trace.append(
                {
                    "step": "decision_failed",
                    "reason": "llm_unavailable",
                    "error": _clip(exc, THOUGHT_CHARS),
                    "prompt": KNOWLEDGE_DECIDE.label,
                }
            )
            return None

        state.ledger.charge(self._llm, prompt, raw)
        state.prompt_label = KNOWLEDGE_DECIDE.label

        try:
            return parse_structured(raw, AgentDecision)
        except StructuredOutputError as exc:
            logger.warning("客服 Agent 决策输出不可解析，转为确定性序列: %s", exc)
            state.trace.append(
                {
                    "step": "decision_failed",
                    "reason": "unparseable",
                    "error": _clip(exc, THOUGHT_CHARS),
                    "prompt": KNOWLEDGE_DECIDE.label,
                }
            )
            return None

    # ── 确定性兜底路径 ────────────────────────────────────────────────────────

    async def _run_deterministic(self, state: _AskState, *, resume: bool = False) -> None:
        """固定顺序检索 FAQ 与规则，再从命中语料拼出答复。

        这不是「功能降级」：它跑的是同一批工具、产出同一个结构，
        事实层本来就不依赖模型。没有模型时损失的是**措辞的灵活度**，
        不是「答不答得出来」。
        """
        state.decision_engine = "rule"
        if not resume:
            state.prompt_label = None
            state.step_index = 0

        plan: List[AgentDecision] = []
        if state.intent == policy.INTENT_PRODUCT:
            # 产品类问题**不查 FAQ 域**：产品文档只在 lookup_product 里，
            # 而 FAQ 域里「一类账户和二类账户有什么区别」这种问句
            # 与「信用卡和借记卡有什么区别」词面高度重叠，
            # 一定会被召回并混进答复。只查规则域拿合规口径就够了。
            plan.append(
                AgentDecision(
                    thought="先按产品名精确查产品文档",
                    action=LOOKUP_PRODUCT,
                    params={"product_name": state.question},
                )
            )
            plan.append(
                AgentDecision(
                    thought="再查业务规则与合规口径",
                    action=SEARCH_POLICY,
                    params={"query": state.question, "top_k": 3},
                )
            )
        else:
            plan.append(
                AgentDecision(
                    thought="查业务 FAQ 与材料流程",
                    action=SEARCH_FAQ,
                    params={"query": state.question, "top_k": 4},
                )
            )
            plan.append(
                AgentDecision(
                    thought="查业务规则与合规口径",
                    action=SEARCH_POLICY,
                    params={"query": state.question, "top_k": 3},
                )
            )
        for decision in plan:
            if state.ledger.tokens >= self._budget.max_tokens:
                state.stop_reason = "max_tokens"
                state.truncated = True
                return
            if state.tool_calls >= self._budget.max_tool_calls:
                state.stop_reason = "max_tool_calls"
                state.truncated = True
                return
            state.step_index += 1
            if not await self._execute(state, decision):
                return

        state.answer = self._fallback_answer(state)
        state.stop_reason = "finished"

    # ── 单步执行 ──────────────────────────────────────────────────────────────

    async def _execute(
        self,
        state: _AskState,
        decision: AgentDecision,
        *,
        allow_when_exhausted: bool = False,
    ) -> bool:
        """执行一步工具调用。返回 ``False`` 表示应当立即收敛。

        ``allow_when_exhausted`` 给出口闸门用：预算耗尽时也要能把
        「需要人工」这件事记下来，否则会出现「明明该转人工但没转」的记录。
        """
        name = decision.action.strip()
        step: Dict[str, Any] = {
            "step": state.step_index,
            "thought": _clip(decision.thought, THOUGHT_CHARS),
            "tool": name,
            "params": dict(decision.params),
            "prompt": state.prompt_label,
        }

        if name not in KNOWLEDGE_TOOL_WHITELIST:
            state.rejections += 1
            step.update(
                {
                    "executed": False,
                    "rejected": "not_whitelisted",
                    "error": f"工具不在白名单内: {name}",
                }
            )
            state.trace.append(step)
            logger.warning("客服 Agent 越权工具调用被拒绝: %s", name)
            if state.rejections >= MAX_REJECTIONS:
                state.stop_reason = "too_many_rejections"
                state.truncated = True
                return False
            return True

        problem = self._tools.validate_params(name, decision.params)
        if problem is not None:
            state.rejections += 1
            step.update({"executed": False, "rejected": "invalid_params", "error": problem})
            state.trace.append(step)
            if state.rejections >= MAX_REJECTIONS:
                state.stop_reason = "too_many_rejections"
                state.truncated = True
                return False
            return True

        signature = json.dumps([name, decision.params], sort_keys=True, default=str)
        state.signatures[signature] = state.signatures.get(signature, 0) + 1
        if state.signatures[signature] > MAX_IDENTICAL_CALLS:
            step.update(
                {
                    "executed": False,
                    "rejected": "repeat_call",
                    "error": "同一工具与参数重复调用，判定为原地打转",
                }
            )
            state.trace.append(step)
            state.stop_reason = "repeat_call"
            state.truncated = True
            return False

        tool_trace: List[Dict[str, Any]] = []
        started = time.monotonic()
        result = await self._tools.call(name, decision.params, {}, trace=tool_trace)
        latency_ms = (time.monotonic() - started) * 1000

        state.tool_calls += 1
        step.update(
            {
                "executed": True,
                "ok": bool(result.success),
                "error": _clip(result.error, THOUGHT_CHARS) or None,
                "observation": summarize_observation(result.data, OBSERVATION_CHARS),
                "latency_ms": round(latency_ms, 1),
                "cached": bool(result.cached),
                "tool_events": tool_trace,
            }
        )
        state.trace.append(step)
        self._absorb(state, name, result.data, result.success)

        if name == HANDOFF_TO_HUMAN and result.success and isinstance(result.data, dict):
            state.handoff = result.data
            # 收敛原因必须在这里落定：调用方看到 ``_execute`` 返回 False 就直接 return，
            # 不会再有别的机会写 stop_reason。漏了这一步，转人工的记录
            # 会带着 ``finished`` 出库 —— 事后统计「转人工率」时会全部漏掉。
            state.stop_reason = "escalated"
            return False
        return True

    @staticmethod
    def _absorb(state: _AskState, name: str, data: Any, ok: bool) -> None:
        """把工具产出吸收进累计证据，并**滤掉话题不相关的召回**。

        为什么要滤：本服务的检索分数是相对归一化的，最高分恒为 1.000，
        所以「今天天气怎么样」也会带回一批满分命中。不滤的话，
        一段看似有出处的答复实际上是拿无关文档拼的 ——
        接地检查过得去，内容却是错的。这比明显的幻觉更难发现。

        过滤判据是词面覆盖率（:data:`~ai_service.knowledge.policy.MIN_TERM_COVERAGE`），
        实测标定过的绝对量，不是拍出来的。
        """
        if not ok:
            return

        candidates: List[Dict[str, Any]] = []
        if name in {SEARCH_FAQ, SEARCH_POLICY} and isinstance(data, list):
            candidates = [item for item in data if isinstance(item, dict)]
        elif name == LOOKUP_PRODUCT and isinstance(data, dict):
            candidates = [
                item for item in (data.get("matches") or []) if isinstance(item, dict)
            ]

        for hit in candidates:
            if not hit.get("doc_id"):
                continue
            if name == LOOKUP_PRODUCT:
                # 精确查表的命中是**关键词命中**，不是检索排序 ——
                # 再拿词面覆盖率去打折会让「信用卡和借记卡有什么区别」
                # 只留下其中一张卡（两者覆盖率略有差异），答成半截。
                # 问的是对比，就该两张都给。
                coverage = EXACT_MATCH_COVERAGE
            else:
                coverage = policy.term_coverage(
                    state.question,
                    [str(hit.get("title") or "") + str(hit.get("content") or "")],
                )
            if coverage < policy.MIN_TERM_COVERAGE:
                state.off_topic_dropped += 1
                continue
            # 记下覆盖率：后面挑「事实段落」时按它排序，
            # 并对明显弱于最佳命中的文档做相对过滤
            stored = dict(hit)
            stored["_coverage"] = round(coverage, 4)
            state.evidence[str(hit["doc_id"])] = stored

    # ── 措辞 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _ranked_evidence(state: _AskState) -> List[Dict[str, Any]]:
        """兼容入口，实际排序在外面的 :func:`rank_evidence` 里。"""
        return rank_evidence(state.evidence)

    def _fallback_answer(self, state: _AskState) -> str:
        """无模型时的确定性答复：直接从命中语料组织段落。

        刻意不写成「AI 不可用，请稍后再试」—— 事实层是齐的，
        损失的应该是措辞的灵活度，而不是可用性。

        组织方式：最相关那条的**导语定题**，其余段落按客户关心的顺序合并去重。
        只让最相关的那条出「业务口径」，否则会把三四个不同话题的口径
        并排摆出来 —— 那种答复读起来像检索结果列表，不像在回答问题。
        """
        citations = self._ranked_evidence(state)
        if not citations:
            state.blocked_by = state.blocked_by or "no_evidence"
            state.actions = state.actions or [
                "知识库中没有可核对的相关内容，未作推测回答，建议转人工核实。"
            ]
            return policy.INSUFFICIENT_ANSWER

        top = citations[0]
        lead, top_sections = corpus_split(top.get("content", ""))

        lines: List[str] = []
        title = str(top.get("title") or "")
        summary = top_sections.get("业务口径") or lead
        if title and summary:
            lines.append(f"关于「{title}」：{summary.strip()}")
        elif summary:
            lines.append(summary.strip())

        # 事实性段落：顺序即客户关心的顺序，跨文档去重。
        # 跳过「业务口径」—— 它已经由上面那条最相关文档的导语承担了，
        # 再逐篇列出来会把答复变成检索结果列表，而不是在回答问题。
        seen: set[str] = set()
        facts = 0
        for label in _SECTION_ORDER:
            if label == "业务口径":
                continue
            for item in citations[:FALLBACK_CITATION_LIMIT]:
                if facts >= MAX_FACT_LINES:
                    break
                _, sections = corpus_split(item.get("content", ""))
                text = (sections.get(label) or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                lines.append(f"{label}：{text}")
                facts += 1

        if facts == 0:
            # 只有导语、没有任何事实段落 —— 说明命中的语料不含可用的结构化内容。
            # 与其返回一句话，不如如实说答不出。
            state.blocked_by = state.blocked_by or "no_evidence"
            return policy.INSUFFICIENT_ANSWER

        lines.append(
            "如需进一步确认，建议通过官方客服或营业网点核实；"
            "账户数据与个性化授信问题不在本渠道处理范围内。"
        )
        return "\n".join(lines)

    @staticmethod
    def _history_block(state: _AskState) -> str:
        if not state.trace:
            return "（这是第一步）"
        lines: List[str] = []
        for entry in state.trace:
            if "tool" not in entry:
                if entry.get("step") == "decision_failed":
                    lines.append(f"- 决策失败：{entry.get('reason', '未知')}")
                continue
            outcome = entry.get("observation") or entry.get("error") or ""
            lines.append(
                f"- 第 {entry.get('step')} 步：调用 {entry['tool']}"
                f" → {_clip(outcome, HISTORY_OBSERVATION_CHARS)}"
            )
        return "\n".join(lines) or "（这是第一步）"

    # ── 结果组装 ──────────────────────────────────────────────────────────────

    def _finalize(self, state: _AskState, started: float) -> KnowledgeOutcome:
        citations = state.citations
        answer = state.answer or policy.INSUFFICIENT_ANSWER

        outcome = KnowledgeOutcome(
            question=state.question,
            intent=state.intent,
            answer=answer,
            citations=citations,
            actions=state.actions,
            handoff=state.handoff,
            refused=state.refused,
            rejected_answer=state.rejected_answer,
            blocked_by=state.blocked_by,
            sanitized=state.sanitized,
            grounding=state.grounding
            or policy.audit_grounding(answer, citations).as_dict(),
            off_topic_dropped=state.off_topic_dropped,
            degraded=state.decision_engine != "llm",
            truncated=state.truncated,
            stop_reason=state.stop_reason,
            decision_engine=state.decision_engine,
            budget=self._budget.as_dict(),
            budget_used={
                "steps": state.step_index,
                "tool_calls": state.tool_calls,
                "rejections": state.rejections,
                "tokens": state.ledger.tokens,
            },
            token_usage=state.ledger.report(),
            prompt_versions={
                "registry": PROMPT_REGISTRY_VERSION,
                "used": collect_labels(state.trace),
            },
            latency_ms=round((time.monotonic() - started) * 1000, 1),
            trace=state.trace,
            tools=self._tools.get_stats(),
            retrieval_backend=self._retrieval_backend,
            llm=self._llm.name,
            llm_available=self._llm.available,
        )
        # 把本轮记进历史，并回传给调用方。记的是**已经收敛的结论**
        # （意图、引用、是否拒答、被哪道闸门拦下），不是模型被拦下的草稿 ——
        # 那份草稿只该留在本次响应里供审计，不该跟着历史往外走。
        updated = state.session.append(Turn.from_outcome(state.question, outcome.to_dict()))
        outcome.history = updated.to_payload()
        return outcome


def corpus_split(content: str) -> tuple[str, Dict[str, str]]:
    """把语料正文切成「导语 + 标签段落」。

    直接复用审核侧的 :func:`ai_service.explain.split_labeled_sections` ——
    两边语料格式一致（``标签：内容`` 逐行），解析器就不该有两份。
    """
    from ai_service.explain import split_labeled_sections

    return split_labeled_sections(content)


def build_knowledge_agent(
    *,
    llm: Optional[LLMClient] = None,
    tool_manager: Optional[ToolManager] = None,
    budget: Optional[AgentBudget] = None,
    faq: Optional[KnowledgeRetriever] = None,
    policy_index: Optional[KnowledgeRetriever] = None,
) -> KnowledgeAgent:
    """组装一个可用的客服 Agent。默认灌入全部内置语料与四个白名单工具。"""
    active_llm: LLMClient = llm or NullLLMClient()
    faq_index = faq or faq_retriever()
    policy_docs = policy_index or policy_retriever()
    active_tools = tool_manager or ToolManager(active_llm)

    build_knowledge_tools(active_tools, faq=faq_index, policy=policy_docs)
    backend = (
        f"faq:{faq_index.vector_backend_name}"
        if faq_index.vector_backend_name == policy_docs.vector_backend_name
        else f"faq:{faq_index.vector_backend_name}|policy:{policy_docs.vector_backend_name}"
    )
    return KnowledgeAgent(
        tools=active_tools,
        llm=active_llm,
        budget=budget,
        retrieval_backend=backend,
    )


async def run_knowledge_ask(
    question: str,
    *,
    llm: Optional[LLMClient] = None,
    budget: Optional[AgentBudget] = None,
    agent: Optional[KnowledgeAgent] = None,
    history: Optional[SessionHistory] = None,
) -> Dict[str, Any]:
    """一次性问答，返回字典。供 ``api.py`` / CLI 调用。"""
    active = agent or build_knowledge_agent(llm=llm, budget=budget)
    outcome = await active.ask(question, history=history)
    return outcome.to_dict()


__all__ = (
    "DEFAULT_MAX_STEPS",
    "FINISH",
    "KNOWLEDGE_AGENT_PROMPT_ID",
    "KnowledgeAgent",
    "KnowledgeBudget",
    "KnowledgeOutcome",
    "SessionHistory",
    "build_knowledge_agent",
    "corpus_split",
    "run_knowledge_ask",
)
