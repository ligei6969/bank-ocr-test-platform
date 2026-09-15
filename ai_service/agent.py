"""审核 Agent：planner + executor 的 tool-calling 循环。

与 P0 的区别
------------
P0 的 ``explain`` 是**固定流水线**：改写 → 召回 → 重排 → 生成，每条记录都走这四步。
本模块是**多步决策**：给一条记录和一个问题，Agent 自己决定查什么、调哪个工具、走几步，
到信息够了或者超预算为止。

```
记录 + 问题
    │
    ▼  ┌──────────────────────────────────────────┐
       │ 1. planner：决定下一步（LLM 结构化输出）  │
       │ 2. 白名单校验 → schema 校验 → 拒绝/放行    │
       │ 3. executor：调工具，拿观察结果            │
       │ 4. 预算检查（步数 / token / 工具调用次数） │
       │ 5. 收敛判定：finish / escalate / 超预算    │
       └──────────────────────────────────────────┘
    │
    ▼
结构化答复 + 完整 trace
```

四条不可破的约束
----------------
1. **白名单是硬的。** 名字不在 :data:`ai_service.tools.TOOL_WHITELIST` 里一律拒绝。
   prompt 说「只能用这些工具」是软约束，模型可以不听；白名单不听就执行不了。
2. **参数必须过 schema。** 校验失败直接拒绝，**不调用工具**（``validate_params``
   在调用前拦下来），避免「参数错了但工具已经产生副作用」。
3. **三重预算，超了收敛不抛异常。** 步数、token、工具调用次数任一超限即停，
   ``truncated=True`` 照常返回已有结果 —— Agent 超预算是正常结局，不是故障。
4. **每一步都能降级。** LLM 不可用或决策解析失败时，走**确定性固定工具序列**，
   产出与 LLM 路径**结构完全一致**的结果，只有 ``engine.decision`` 标记为 ``rule``。

关于 token 计量
---------------
本服务的 ``LLMClient`` 协议只返回文本，拿不到 provider 的 usage 字段，
所以这里用字符估算（CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token）。
它的用途是**预算兜底**（防止模型无限自问自答烧钱），不是计费依据。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from pydantic import BaseModel, Field

from ai_service.corpus import KnowledgeDoc, reason_code_lookup
from ai_service.explain import (
    DEFAULT_QUESTION,
    DISCLAIMER,
    ReviewContext,
    build_actions,
    build_reason_details,
    render_template_explanation,
    to_citation,
)
from ai_service.llm import LLMClient, LLMUnavailableError, NullLLMClient
from ai_service.prompts import AGENT_DECIDE, PROMPT_REGISTRY_VERSION, collect_labels
from ai_service.structured import StructuredOutputError, parse_structured
from ai_service.tool_manager import ToolManager
from ai_service.tools import (
    ESCALATE_TO_HUMAN,
    GET_REVIEW_RECORD,
    RECOMPUTE_QUALITY,
    SEARCH_KNOWLEDGE,
    TOOL_WHITELIST,
    tool_catalog_text,
)

logger = logging.getLogger(__name__)

FINISH = "finish"

DEFAULT_MAX_STEPS = 6
DEFAULT_STEP_TIMEOUT_S = 5.0
DEFAULT_MAX_TOKENS = 3000
DEFAULT_MAX_TOOL_CALLS = 8

#: 连续被拒（非法工具名 / 参数不合法）多少次就直接收敛。
#: 不设上限的话，一个执着于调非法工具的模型能把预算烧干净。
MAX_REJECTIONS = 3

#: 同一「工具 + 参数」最多重复几次。超过判定为原地打转，直接收敛。
MAX_IDENTICAL_CALLS = 2

#: 进 trace 与进 prompt 的文本截断长度。观察结果只留摘要，避免 prompt 膨胀。
THOUGHT_CHARS = 200
OBSERVATION_CHARS = 400
HISTORY_OBSERVATION_CHARS = 220


# ── 决策对象 ──────────────────────────────────────────────────────────────────

class AgentDecision(BaseModel):
    """单步决策。

    ``action`` 刻意用 ``str`` 而不是 ``Literal[...]``：用 ``Literal`` 的话，
    非法工具名会在 pydantic 校验阶段就变成「解析失败」，trace 里只剩一条
    「模型输出不合规」，看不出「它想调哪个越权工具」。用 ``str`` 接住，
    再显式做白名单校验，才能留下可审计的拒绝记录。
    """

    thought: str = ""
    action: str = Field(min_length=1)
    params: Dict[str, Any] = Field(default_factory=dict)
    answer: str = ""


@dataclass(frozen=True)
class AgentBudget:
    """三重预算。任一超限即收敛。"""

    max_steps: int = DEFAULT_MAX_STEPS
    step_timeout_s: float = DEFAULT_STEP_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "step_timeout_s": self.step_timeout_s,
            "max_tokens": self.max_tokens,
            "max_tool_calls": self.max_tool_calls,
        }


@dataclass
class AgentOutcome:
    """Agent 的最终产出。LLM 路径与降级路径共用这一个结构。"""

    request_id: str
    doc_type: str
    review_result: str
    question: str
    answer: str
    reason_details: List[Dict[str, Any]] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    citations: List[Dict[str, Any]] = field(default_factory=list)
    unknown_reason_codes: List[str] = field(default_factory=list)
    escalation: Optional[Dict[str, Any]] = None
    quality_audit: Optional[Dict[str, Any]] = None
    decision_engine: str = "rule"
    degraded: bool = True
    truncated: bool = False
    stop_reason: str = "finished"
    budget: Dict[str, Any] = field(default_factory=dict)
    budget_used: Dict[str, Any] = field(default_factory=dict)
    prompt_versions: Dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    trace: List[Dict[str, Any]] = field(default_factory=list)
    tools: Dict[str, Any] = field(default_factory=dict)
    retrieval_backend: str = ""
    llm: str = "none"
    llm_available: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """对外结构。``answer`` 同时以 ``explanation`` 暴露，方便前端复用 P0 面板。"""
        return {
            "request_id": self.request_id,
            "doc_type": self.doc_type,
            "review_result": self.review_result,
            "question": self.question,
            "answer": self.answer,
            "explanation": self.answer,
            "reason_details": self.reason_details,
            "actions": self.actions,
            "citations": self.citations,
            "unknown_reason_codes": self.unknown_reason_codes,
            "escalation": self.escalation,
            "quality_audit": self.quality_audit,
            "degraded": self.degraded,
            "truncated": self.truncated,
            "stop_reason": self.stop_reason,
            "engine": {
                "llm": self.llm,
                "llm_available": self.llm_available,
                "decision": self.decision_engine,
                "retrieval": self.retrieval_backend,
            },
            "budget": self.budget,
            "budget_used": self.budget_used,
            "prompt_versions": self.prompt_versions,
            "latency_ms": self.latency_ms,
            "trace": self.trace,
            "tools": self.tools,
            "disclaimer": DISCLAIMER,
        }


def estimate_tokens(text: str) -> int:
    """字符级 token 估算，用于预算兜底。

    CJK 大致 1 字 1 token，拉丁文约 4 字符 1 token。不追求准确 ——
    它只需要「随输出长度单调增长」，好让预算能真的封顶。
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(0, other) // 4


def _clip(text: Any, limit: int) -> str:
    """截断长文本。trace 与 prompt 都走它，避免单条观察把上下文撑爆。"""
    value = "" if text is None else str(text)
    value = value.replace("\n", " ").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _summarize(data: Any, limit: int = OBSERVATION_CHARS) -> str:
    """把工具返回压成一句可读摘要。"""
    if isinstance(data, list):
        titles = [
            str(item.get("title") or item.get("doc_id") or "")
            for item in data[:4]
            if isinstance(item, dict)
        ]
        head = "、".join(title for title in titles if title)
        return _clip(f"{len(data)} 条结果：{head}", limit)
    if isinstance(data, dict):
        if data.get("error") == "not_found":
            return "记录不存在（只能读本次请求对应的记录）"
        if data.get("escalated"):
            return _clip(f"已请求人工：{data.get('reason', '')}", limit)
        if data.get("mode"):
            return _clip(
                f"质检核对 mode={data['mode']} recomputed={data.get('recomputed')}",
                limit,
            )
        return _clip(json.dumps(data, ensure_ascii=False, default=str), limit)
    return _clip(data, limit)


# ── Agent ─────────────────────────────────────────────────────────────────────

class ReviewAgent:
    """会调工具、会多步决策的审核 Agent。"""

    def __init__(
        self,
        *,
        tools: ToolManager,
        llm: Optional[LLMClient] = None,
        reason_lookup: Optional[Mapping[str, KnowledgeDoc]] = None,
        budget: Optional[AgentBudget] = None,
        retrieval_backend: str = "",
    ) -> None:
        self._tools = tools
        self._llm: LLMClient = llm or NullLLMClient()
        self._reason_lookup: Mapping[str, KnowledgeDoc] = (
            dict(reason_lookup) if reason_lookup is not None else reason_code_lookup()
        )
        self._budget = budget or AgentBudget()
        self._retrieval_backend = retrieval_backend

    @property
    def tools(self) -> ToolManager:
        """工具管理器。暴露出来是为了让测试能替换某个工具的 handler
        （故障注入），而不必去捅 ``_tools``。"""
        return self._tools

    @property
    def budget(self) -> AgentBudget:
        return self._budget

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def run(self, context: ReviewContext, *, question: str = "") -> AgentOutcome:
        """跑完一轮 Agent，返回结构化结果。**任何情况下都不抛异常。**"""
        started = time.monotonic()
        state = _LoopState(question=(question or context.question or DEFAULT_QUESTION).strip())

        if self._llm.available:
            await self._run_llm_loop(context, state)
        else:
            logger.info("LLM 不可用，Agent 走确定性工具序列")
            await self._run_deterministic(context, state)

        return self._finalize(context, state, started)

    # ── LLM 决策循环 ──────────────────────────────────────────────────────────

    async def _run_llm_loop(self, context: ReviewContext, state: "_LoopState") -> None:
        budget = self._budget
        for step_index in range(1, budget.max_steps + 1):
            state.step_index = step_index
            if state.tool_calls >= budget.max_tool_calls:
                state.stop_reason = "max_tool_calls"
                state.truncated = True
                return
            if state.tokens >= budget.max_tokens:
                state.stop_reason = "max_tokens"
                state.truncated = True
                return

            decision = await self._decide(context, state)
            if decision is None:
                # 决策拿不到（调用失败 / 输出不可解析）→ 退回确定性路径，
                # 而不是把「模型抽风」变成「整条链路失败」。
                state.decision_engine = "rule"
                state.decision_failed = True
                await self._run_deterministic(context, state, resume=True)
                return

            if decision.action == FINISH:
                # 收敛也是模型的一次决策，trace 里要留痕，否则「这轮到底调过模型吗」
                # 在只查 trace 时无从判断
                state.trace.append(
                    {
                        "step": state.step_index,
                        "kind": "finish",
                        "thought": _clip(decision.thought, THOUGHT_CHARS),
                        "prompt": state.prompt_label,
                    }
                )
                state.answer = decision.answer.strip() or self._fallback_answer(context, state)
                state.stop_reason = "finished"
                # 规则优先于模型：证据不足时必须转人工。
                # 这一步不能只写在确定性路径里 —— 否则模型（或被 prompt injection
                # 影响的模型）一句 finish 就把转人工规则跳过去了，规则形同虚设。
                await self._enforce_escalation_rule(context, state)
                return

            if not await self._execute(context, state, decision):
                # 连续拒绝过多 / 原地打转 → 已经设置好 stop_reason
                return

        state.stop_reason = "max_steps"
        state.truncated = True

    async def _decide(
        self,
        context: ReviewContext,
        state: "_LoopState",
    ) -> Optional[AgentDecision]:
        """一次 planner 调用。失败返回 ``None``，由调用方决定怎么降级。"""
        prompt = AGENT_DECIDE.render(
            question=state.question,
            context_block=self._context_block(context),
            tool_catalog=tool_catalog_text(),
            history=self._history_block(state),
        )
        try:
            raw = await self._llm.complete(
                prompt,
                system=AGENT_DECIDE.system,
                max_tokens=512,
                temperature=0.0,
            )
        except LLMUnavailableError as exc:
            logger.warning("Agent 决策调用失败，转为确定性序列: %s", exc)
            state.trace.append(
                {
                    "step": "decision_failed",
                    "reason": "llm_unavailable",
                    "error": _clip(exc, THOUGHT_CHARS),
                    "prompt": AGENT_DECIDE.label,
                }
            )
            return None

        state.tokens += estimate_tokens(prompt) + estimate_tokens(raw)
        state.prompt_label = AGENT_DECIDE.label

        try:
            return parse_structured(raw, AgentDecision)
        except StructuredOutputError as exc:
            logger.warning("Agent 决策输出不可解析，转为确定性序列: %s", exc)
            state.trace.append(
                {
                    "step": "decision_failed",
                    "reason": "unparseable",
                    "error": _clip(exc, THOUGHT_CHARS),
                    "prompt": AGENT_DECIDE.label,
                }
            )
            return None

    # ── 单步执行（两条路径共用）──────────────────────────────────────────────

    async def _execute(
        self,
        context: ReviewContext,
        state: "_LoopState",
        decision: AgentDecision,
    ) -> bool:
        """执行一步工具调用。返回 ``False`` 表示应当立即收敛。"""
        name = decision.action.strip()
        step: Dict[str, Any] = {
            "step": state.step_index,
            "thought": _clip(decision.thought, THOUGHT_CHARS),
            "tool": name,
            "params": dict(decision.params),
            "prompt": state.prompt_label,
        }

        if name not in TOOL_WHITELIST:
            state.rejections += 1
            step.update(
                {
                    # 用 executed 而不是 ok=False：这两件事在审计上完全不同 ——
                    # 「拒绝了」不产生任何后果，「执行失败」可能产生后果。
                    "executed": False,
                    "rejected": "not_whitelisted",
                    "error": f"工具不在白名单内: {name}",
                }
            )
            state.trace.append(step)
            logger.warning("Agent 越权工具调用被拒绝: %s", name)
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
        result = await self._tools.call(
            name,
            decision.params,
            self._tool_context(context),
            trace=tool_trace,
        )
        latency_ms = (time.monotonic() - started) * 1000

        state.tool_calls += 1
        step.update(
            {
                "executed": True,
                "ok": bool(result.success),
                "error": _clip(result.error, THOUGHT_CHARS) or None,
                "observation": _summarize(result.data),
                "latency_ms": round(latency_ms, 1),
                "cached": bool(result.cached),
                "tool_events": tool_trace,
            }
        )
        state.trace.append(step)
        self._absorb(state, name, result.data, result.success)

        if name == ESCALATE_TO_HUMAN and result.success and isinstance(result.data, dict):
            state.escalation = result.data
            suffix = self._escalation_suffix(result.data)
            # 模型已经写过答复就在后面补一句转人工，而不是把它的措辞丢掉
            state.answer = (
                f"{state.answer}{suffix}"
                if state.answer
                else f"{self._fallback_answer(context, state)}{suffix}"
            )
            state.stop_reason = "escalated"
            return False
        return True

    @staticmethod
    def _absorb(state: "_LoopState", name: str, data: Any, ok: bool) -> None:
        """把工具产出吸收进累计证据。"""
        if not ok:
            return
        if name == SEARCH_KNOWLEDGE and isinstance(data, list):
            for hit in data:
                if isinstance(hit, dict) and hit.get("doc_id"):
                    state.evidence[str(hit["doc_id"])] = hit
        elif name == RECOMPUTE_QUALITY and isinstance(data, dict):
            state.quality_audit = data

    # ── 确定性降级路径 ────────────────────────────────────────────────────────

    async def _run_deterministic(
        self,
        context: ReviewContext,
        state: "_LoopState",
        *,
        resume: bool = False,
    ) -> None:
        """固定工具序列：读记录 → 查知识 → 核质检 → 需要时举手找人。

        这不是「功能降级」，而是**另一条同样完整的路径**：它跑的是同一批工具、
        产出同一个结构，只少了模型自主选路的能力。事实层本来就不依赖模型。
        """
        reason_codes = context.all_reason_codes
        state.decision_engine = "rule"
        if not resume:
            state.prompt_label = None
        # 降级路径不调用模型，因此不消耗 token —— 这是有意的，不是漏算

        plan: List[AgentDecision] = [
            AgentDecision(
                thought="先确认记录里到底有什么",
                action=GET_REVIEW_RECORD,
                params={"request_id": context.request_id},
            )
        ]
        if reason_codes:
            plan.append(
                AgentDecision(
                    thought="记录里有原因码，去知识库查释义与处置",
                    action=SEARCH_KNOWLEDGE,
                    params={"query": self._search_query(context), "top_k": 5},
                )
            )
        if context.quality_result or context.quality_reasons:
            plan.append(
                AgentDecision(
                    thought="有影像质量结论，核对阈值是否自洽",
                    action=RECOMPUTE_QUALITY,
                    params={},
                )
            )

        for decision in plan:
            if state.step_index >= self._budget.max_steps:
                # 确定性序列也要受步数预算约束：预算不是「LLM 路径专属」的
                state.stop_reason = "max_steps"
                state.truncated = True
                return
            state.step_index += 1
            if not await self._execute(context, state, decision):
                return

        await self._enforce_escalation_rule(context, state)

    async def _enforce_escalation_rule(
        self,
        context: ReviewContext,
        state: "_LoopState",
    ) -> None:
        """规则优先于模型：证据不足时必须转人工。

        只写在确定性路径里是不够的 —— 那样模型（或被 prompt injection 影响的
        模型）只要回一个 ``finish`` 就能把这条规则跳过去。所以两条路径在收敛前
        都必须过这里。

        唯一例外是**预算耗尽**：那时不再增加步骤，只如实标记 ``truncated=True``。
        预算的语义是「超了就停」，为了让规则生效而偷偷多走一步，会让预算变成
        一个不可信的承诺。
        """
        self._facts(context, state)
        if state.escalation is not None:
            return
        if not self._needs_human(context, state):
            return

        state.step_index += 1
        await self._execute(
            context,
            state,
            AgentDecision(
                thought="证据不足，交给人工",
                action=ESCALATE_TO_HUMAN,
                params={
                    "reason": self._escalation_reason(context, state),
                    "missing_evidence": self._missing_evidence(context, state),
                },
            ),
        )

    def _needs_human(self, context: ReviewContext, state: "_LoopState") -> bool:
        """什么时候该举手：没原因码、有未收录原因码、或关键证据没取到。"""
        if not context.all_reason_codes:
            # 没有原因码时，只有**显式 pass** 是自解释的（质检与字段都无异常）。
            # 结论为空 / review / reject / error 却没有任何原因码，说明这条记录
            # 的判定来路不明，不能替审核员编一个解释，交人工。
            return context.review_result != "pass"
        if state.unknown_codes:
            return True
        return not state.evidence and not state.quality_audit

    @staticmethod
    def _escalation_reason(context: ReviewContext, state: "_LoopState") -> str:
        if state.unknown_codes:
            return f"存在知识库未收录的原因码：{'、'.join(state.unknown_codes)}"
        if not context.all_reason_codes:
            return f"记录结论为 {context.review_result or '未知'} 但没有原因码，无法定位根因"
        return "未能取到足够的支撑材料"

    @staticmethod
    def _missing_evidence(context: ReviewContext, state: "_LoopState") -> List[str]:
        missing: List[str] = []
        if not state.evidence:
            missing.append("检索到的知识片段")
        if not state.quality_audit and context.quality_reasons:
            missing.append("影像质量阈值核对结果")
        if state.unknown_codes:
            missing.append(f"原因码释义：{'、'.join(state.unknown_codes)}")
        return missing

    # ── 措辞 ──────────────────────────────────────────────────────────────────

    def _fallback_answer(self, context: ReviewContext, state: "_LoopState") -> str:
        """确定性措辞。复用 explain 的模板，保证两条路事实口径一致。"""
        details, actions = self._facts(context, state)
        return render_template_explanation(context, details, actions)

    @staticmethod
    def _escalation_suffix(escalation: Dict[str, Any]) -> str:
        return f"已按「证据不足」流程转人工复核：{escalation.get('reason', '')}"

    # ── 结果组装 ──────────────────────────────────────────────────────────────

    def _finalize(
        self,
        context: ReviewContext,
        state: "_LoopState",
        started: float,
    ) -> AgentOutcome:
        details, actions = self._facts(context, state)
        citations = [to_citation(hit) for hit in state.evidence.values()]

        return AgentOutcome(
            request_id=context.request_id,
            doc_type=context.doc_type,
            review_result=context.review_result,
            question=state.question,
            answer=state.answer or self._fallback_answer(context, state),
            reason_details=details,
            actions=actions,
            citations=citations,
            unknown_reason_codes=state.unknown_codes,
            escalation=state.escalation,
            quality_audit=state.quality_audit,
            decision_engine=state.decision_engine,
            degraded=state.decision_engine != "llm",
            truncated=state.truncated,
            stop_reason=state.stop_reason,
            budget=self._budget.as_dict(),
            budget_used={
                "steps": state.step_index,
                "tool_calls": state.tool_calls,
                "tokens": state.tokens,
                "rejections": state.rejections,
            },
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

    def _facts(
        self,
        context: ReviewContext,
        state: "_LoopState",
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        """事实层：原因码明细 + 处置建议。与 explain 共用同一份实现。"""
        codes = context.all_reason_codes
        details = build_reason_details(codes, self._reason_lookup)
        state.unknown_codes = [item["code"] for item in details if not item["known"]]
        return details, build_actions(context, details)

    # ── prompt 素材 ───────────────────────────────────────────────────────────

    @staticmethod
    def _context_block(context: ReviewContext) -> str:
        """记录上下文。只放平台已经发过来的字段，不额外捞任何东西。"""
        lines = [
            f"request_id：{context.request_id}",
            f"证件类型：{context.doc_type}",
            f"审核结论：{context.review_result or '未知'}",
            f"质检结论：{context.quality_result or '无'}",
            f"质检原因码：{'、'.join(context.quality_reasons) or '无'}",
            f"审核原因码：{'、'.join(context.review_reasons) or '无'}",
            f"错误信息：{context.error_message or '无'}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _history_block(state: "_LoopState") -> str:
        if not state.trace:
            return "（这是第一步）"
        lines: List[str] = []
        for entry in state.trace:
            if "tool" not in entry:
                # 只把「出问题」的非工具步骤写进 prompt，正常决策不必复述
                if entry.get("step") == "decision_failed":
                    lines.append(f"- 决策失败：{entry.get('reason', '未知')}")
                continue
            outcome = entry.get("observation") or entry.get("error") or ""
            lines.append(
                f"- 第 {entry.get('step')} 步：调用 {entry['tool']}"
                f" → {_clip(outcome, HISTORY_OBSERVATION_CHARS)}"
            )
        return "\n".join(lines) or "（这是第一步）"

    @staticmethod
    def _search_query(context: ReviewContext) -> str:
        codes = " ".join(context.all_reason_codes)
        return f"{context.doc_type} 审核 原因码 {codes} 含义 处置建议 拍摄规范".strip()

    def _tool_context(self, context: ReviewContext) -> Dict[str, Any]:
        return {
            "reason_codes": context.all_reason_codes,
            "doc_type": context.doc_type,
            "quality_result": context.quality_result,
            "quality_reasons": list(context.quality_reasons),
            "quality_metrics": dict(context.quality_metrics),
        }


@dataclass
class _LoopState:
    """循环的累计状态。抽出来是为了让两条路径共用同一份记账。"""

    question: str
    step_index: int = 0
    tool_calls: int = 0
    tokens: int = 0
    rejections: int = 0
    decision_engine: str = "llm"
    decision_failed: bool = False
    truncated: bool = False
    stop_reason: str = "finished"
    answer: str = ""
    prompt_label: Optional[str] = None
    step_step: Dict[str, Any] = field(default_factory=dict)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    evidence: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    signatures: Dict[str, int] = field(default_factory=dict)
    unknown_codes: List[str] = field(default_factory=list)
    quality_audit: Optional[Dict[str, Any]] = None
    escalation: Optional[Dict[str, Any]] = None


def build_agent(
    *,
    llm: Optional[LLMClient] = None,
    tool_manager: Optional[ToolManager] = None,
    reason_lookup: Optional[Mapping[str, KnowledgeDoc]] = None,
    budget: Optional[AgentBudget] = None,
    retriever: Any = None,
    records: Any = None,
) -> ReviewAgent:
    """组装一个可用的 Agent。默认灌入全部内置语料与四个白名单工具。"""
    from ai_service.retrieval import KnowledgeRetriever
    from ai_service.tools import build_review_tools
    from ai_service.corpus import load_documents

    active_llm: LLMClient = llm or NullLLMClient()
    active_retriever = retriever or KnowledgeRetriever(load_documents())
    active_tools = tool_manager or ToolManager(active_llm)
    lookup = dict(reason_lookup) if reason_lookup is not None else reason_code_lookup()

    if records is None:
        from ai_service.tools import ContextRecordSource

        records = ContextRecordSource(request_id="", record={})

    build_review_tools(
        active_tools,
        retriever=active_retriever,
        records=records,
        reason_lookup=lookup,
    )
    return ReviewAgent(
        tools=active_tools,
        llm=active_llm,
        reason_lookup=lookup,
        budget=budget,
        retrieval_backend=active_retriever.vector_backend_name,
    )


async def run_agent_for_context(
    context: ReviewContext,
    *,
    llm: Optional[LLMClient] = None,
    budget: Optional[AgentBudget] = None,
    retriever: Any = None,
) -> Dict[str, Any]:
    """一次性跑完并返回字典。供 ``api.py`` / CLI 调用。

    记录源绑定到本次请求上下文 —— 这决定了 Agent 只能读这一条记录。
    ``retriever`` 可以由调用方复用（例如服务进程里共用一份索引），
    传 ``None`` 时按内置语料现建一份。
    """
    from ai_service.tools import ContextRecordSource

    agent = build_agent(
        llm=llm,
        budget=budget,
        retriever=retriever,
        records=ContextRecordSource(
            request_id=context.request_id,
            record=context.to_record_fields(),
        ),
    )
    outcome = await agent.run(context)
    return outcome.to_dict()


__all__ = (
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MAX_TOKENS",
    "FINISH",
    "MAX_IDENTICAL_CALLS",
    "MAX_REJECTIONS",
    "AgentBudget",
    "AgentDecision",
    "AgentOutcome",
    "ReviewAgent",
    "build_agent",
    "estimate_tokens",
    "run_agent_for_context",
)
