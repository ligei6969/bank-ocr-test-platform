"""解释生成：把「审核记录 + 知识库检索结果」变成审核员能直接用的答复。

核心设计：事实与措辞分离
-----------------------
* **事实层**（``reason_details``）：原因码释义、触发条件、实现位置、阈值，
  全部来自 ``corpus.py`` 的语料，**不经过 LLM**。所以即使没有配模型，
  输出的阈值和处置方向依然准确、可核对。
* **措辞层**（``explanation``）：把事实组织成一段人话。
  有 LLM 时由 LLM 写，没有时由模板拼装，两者输出结构完全一致。

这样做的直接好处是：**降级只损失表达质量，不损失事实准确性**。
很多「LLM 降级」实现会把整段功能一起砍掉，这里不是。

另外，语料正文是按 ``触发条件：`` / ``处置建议：`` 这类标签写的，
本模块用一个通用的标签切分器把它解析成结构化字段 —— 单一事实来源，
避免语料和结构化字段各写一份、日久漂移。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ai_service.corpus import (
    DOC_TYPE_BANK_CARD,
    DOC_TYPE_ID_CARD,
    KnowledgeDoc,
    load_documents,
    reason_code_lookup,
)
from ai_service.llm import LLMClient, LLMUnavailableError, NullLLMClient
from ai_service.prompts import EXPLAIN_GENERATE, PROMPT_REGISTRY_VERSION, collect_labels
from ai_service.retrieval import KnowledgeRetriever
from ai_service.tool_manager import Tool, ToolManager, ToolResult

logger = logging.getLogger(__name__)

TOOL_NAME = "knowledge_search"
DEFAULT_TOP_K = 5
DEFAULT_QUESTION = "这条审核记录为什么是这个结论？应该怎么处置？"

QUALITY_CODES = frozenset({"image_blur", "image_dark", "image_bright", "glare_detected"})
INFRASTRUCTURE_CODES = frozenset(
    {
        "invalid_file_type",
        "unreadable_image",
        "invalid_ocr_mode",
        "invalid_request",
        "internal_error",
    }
)
DOC_TYPE_LABELS = {
    DOC_TYPE_BANK_CARD: "银行卡",
    DOC_TYPE_ID_CARD: "身份证",
}
REVIEW_RESULT_LABELS = {
    "pass": "通过",
    "review": "待人工复核",
    "reject": "拒绝",
    "error": "处理异常",
}

LABEL_PATTERN = re.compile(r"([\u4e00-\u9fff]{2,8}|[A-Za-z_]{2,20})：")
DISCLAIMER = "本解释由 AI 依据审核知识库生成，仅供复核参考，最终结论以人工判断为准。"


# ── 输入 ──────────────────────────────────────────────────────────────────────

@dataclass
class ReviewContext:
    """平台侧脱敏后发过来的审核上下文。"""

    request_id: str
    doc_type: str = DOC_TYPE_BANK_CARD
    review_result: str = ""
    quality_result: Optional[str] = None
    quality_reasons: List[str] = field(default_factory=list)
    review_reasons: List[str] = field(default_factory=list)
    fields: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    ocr_mode: Optional[str] = None
    question: str = ""
    #: 原始影像质量指标（可选）。平台带过来的话 ``recompute_quality`` 就能真重算，
    #: 不带就只能做结论自洽性核对 —— 工具输出里会明示是哪一种。
    quality_metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def all_reason_codes(self) -> List[str]:
        """质量原因码 + 审核原因码，保序去重。"""
        ordered: List[str] = []
        for code in list(self.review_reasons) + list(self.quality_reasons):
            text = str(code).strip()
            if text and text not in ordered:
                ordered.append(text)
        return ordered

    @property
    def effective_question(self) -> str:
        return self.question.strip() or DEFAULT_QUESTION

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "ReviewContext":
        return cls(
            request_id=str(payload.get("request_id", "")),
            doc_type=str(payload.get("doc_type") or DOC_TYPE_BANK_CARD),
            review_result=str(payload.get("review_result") or ""),
            quality_result=(
                str(payload["quality_result"]) if payload.get("quality_result") else None
            ),
            quality_reasons=_string_list(payload.get("quality_reasons")),
            review_reasons=_string_list(payload.get("review_reasons")),
            fields=payload.get("fields") if isinstance(payload.get("fields"), dict) else {},
            error_message=(
                str(payload["error_message"]) if payload.get("error_message") else None
            ),
            ocr_mode=str(payload["ocr_mode"]) if payload.get("ocr_mode") else None,
            question=str(payload.get("question") or ""),
            quality_metrics=(
                payload["quality_metrics"]
                if isinstance(payload.get("quality_metrics"), dict)
                else {}
            ),
        )

    def to_record_fields(self) -> Dict[str, Any]:
        """给 Agent ``get_review_record`` 工具用的记录视图。

        ``fields`` 里含证件号码与姓名，**入参这里的值已经是平台脱敏后的结果**
        （脱敏在 ``app/ai_client.py`` 出站前统一执行）。这里不二次脱敏，
        避免两处逻辑不一致 —— 但也不额外放行任何平台没发过来的字段。
        """
        return {
            "request_id": self.request_id,
            "doc_type": self.doc_type,
            "review_result": self.review_result,
            "quality_result": self.quality_result,
            "quality_reasons": list(self.quality_reasons),
            "review_reasons": list(self.review_reasons),
            "fields": dict(self.fields),
            "error_message": self.error_message,
            "ocr_mode": self.ocr_mode,
        }


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item).strip()]


# ── 语料正文的标签切分 ────────────────────────────────────────────────────────

def split_labeled_sections(text: str) -> tuple[str, Dict[str, str]]:
    """把「前缀说明 + 若干 ``标签：内容``」的正文拆成结构化字段。

    返回 ``(首段, {标签: 内容})``。识别不了标签时首段就是全文、字典为空，
    调用方需要能容忍这种情况。
    """
    matches = list(LABEL_PATTERN.finditer(text))
    if not matches:
        return text.strip(), {}

    lead = text[: matches[0].start()].strip()
    sections: Dict[str, str] = {}
    for index, match in enumerate(matches):
        label = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content and label not in sections:
            sections[label] = content
    return lead, sections


def classify_reason_code(code: str) -> str:
    """把原因码归到三类根因之一：质量 / 字段规则 / 基础设施。"""
    if code in QUALITY_CODES:
        return "quality"
    if code in INFRASTRUCTURE_CODES:
        return "infrastructure"
    return "field"


# ── 事实层（模块级函数）──────────────────────────────────────────────────────
#
# 这三个函数刻意放在模块级而不是 ReviewExplainer 的私有方法里：
# P1 的 Agent 与 P0 的 explain 必须**共用同一套事实**，否则「事实与措辞分离」
# 会退化成「两套事实各自表述」，两边的阈值和处置建议迟早对不上。

def build_reason_details(
    codes: Sequence[str],
    reason_lookup: Mapping[str, KnowledgeDoc],
) -> List[Dict[str, Any]]:
    """按原因码从语料取出结构化事实条目；未收录的码显式标注 ``known=False``。"""
    details: List[Dict[str, Any]] = []
    for code in codes:
        doc = reason_lookup.get(code)
        if doc is None:
            details.append(
                {
                    "code": code,
                    "known": False,
                    "title": f"{code}（语料未收录）",
                    "root_cause": "unknown",
                    "meaning": "该原因码尚未收录到知识库语料中，无法给出准确释义。",
                    "trigger": None,
                    "implementation": None,
                    "advice": None,
                    "user_message": None,
                    "doc_id": None,
                }
            )
            continue

        lead, sections = split_labeled_sections(doc.content)
        details.append(
            {
                "code": code,
                "known": True,
                "title": doc.title,
                "root_cause": classify_reason_code(code),
                "meaning": sections.get("业务含义") or lead,
                "trigger": sections.get("触发条件") or sections.get("为什么重要"),
                "implementation": sections.get("实现位置"),
                "advice": sections.get("处置建议") or sections.get("正确做法"),
                "user_message": sections.get("用户话术"),
                "doc_id": doc.doc_id,
            }
        )
    return details


def build_actions(
    context: "ReviewContext",
    reason_details: Sequence[Dict[str, Any]],
) -> List[str]:
    """跨原因码的专家规则 + 各原因码自带的处置建议。顺序即优先级。"""
    actions: List[str] = []
    known_codes = {item["code"] for item in reason_details}
    has_quality = bool(known_codes & QUALITY_CODES)
    has_field = any(item["root_cause"] == "field" for item in reason_details)
    has_infra = any(item["root_cause"] == "infrastructure" for item in reason_details)

    if has_infra:
        actions.append(
            "本条属于服务端或调用方问题，**不要**向用户发出重拍提示；"
            "按 request_id 报障并核对服务端配置或请求格式。"
        )
    if has_quality and has_field:
        actions.append(
            "质量原因码与字段原因码同时出现，字段缺失大概率是影像质量的下游后果 —— "
            "先修影像质量，不必单独追问用户字段内容。"
        )
    if "glare_detected" in known_codes and not has_field:
        actions.append(
            "仅有反光原因码且字段全部解析成功，高光大概率未遮挡关键字段，"
            "核对位置后可直接放行，并把该记录标记为规则误报样本。"
        )
    if "invalid_card_number" in known_codes and has_quality:
        actions.append(
            "卡号非法同时伴随质量原因码，优先怀疑 OCR 误识而不是伪造卡，"
            "按 review 处理并让用户重拍，不建议直接拒绝。"
        )
    if "image_blur" in known_codes:
        actions.append("让用户重新拍摄时，重点提示对焦与防抖，并建议不要用聊天软件二次传输。")

    for item in reason_details:
        if item.get("advice") and item["advice"] not in actions:
            actions.append(f"{item['code']}：{item['advice']}")

    if not actions:
        if context.review_result == "pass":
            actions.append("本次审核无异常原因码，无需额外处置。")
        else:
            actions.append("未匹配到具体原因码，建议人工核对影像与解析字段后再判定。")

    return actions


def to_citation(hit: Dict[str, Any]) -> Dict[str, Any]:
    """把检索命中压成给前端展示的引用条目（正文截断到 160 字）。"""
    content = str(hit.get("content", ""))
    return {
        "doc_id": hit.get("doc_id"),
        "title": hit.get("title", ""),
        "category": hit.get("category", ""),
        "score": hit.get("score", 0.0),
        "matched_reason_codes": hit.get("matched_reason_codes", []),
        "retrieval_channels": hit.get("retrieval_channels", []),
        "snippet": content[:160] + ("…" if len(content) > 160 else ""),
        "content": content,
    }


def render_template_explanation(
    context: "ReviewContext",
    reason_details: Sequence[Dict[str, Any]],
    actions: Sequence[str],
) -> str:
    """无 LLM 时的确定性措辞。

    刻意写得完整可读，而不是一句「AI 不可用」——
    因为事实层本来就是齐的，损失的不该是可用性。

    Agent 的确定性降级路径复用这一段，保证两条路的措辞风格与事实口径一致。
    """
    doc_label = DOC_TYPE_LABELS.get(context.doc_type, context.doc_type)
    result_label = REVIEW_RESULT_LABELS.get(
        context.review_result, context.review_result or "未知"
    )
    sentences: List[str] = [f"本次{doc_label}审核的结论是「{result_label}」。"]

    if not reason_details:
        sentences.append(
            "记录中没有审核原因码，说明质量检测与字段解析均无异常，"
            "结论由规则判定直接给出，无需额外处置。"
        )
        return "".join(sentences)

    root_causes = {item["root_cause"] for item in reason_details}
    root_labels = {
        "quality": "影像质量层",
        "field": "字段解析层",
        "infrastructure": "服务端或调用方",
        "unknown": "未收录层",
    }
    layers = "、".join(root_labels.get(item, item) for item in sorted(root_causes))
    sentences.append(f"共命中 {len(reason_details)} 个原因码，根因涉及{layers}。")

    for item in reason_details:
        if not item["known"]:
            sentences.append(f"原因码 {item['code']} 尚未收录语料，需人工确认含义。")
            continue
        meaning = _first_sentence(item.get("meaning") or "")
        sentences.append(f"{item['code']}：{meaning}")

    if actions:
        sentences.append(f"处置方向：{actions[0]}")

    return "".join(sentences)


# ── 解释器 ────────────────────────────────────────────────────────────────────

class ReviewExplainer:
    """编排：构造查询 → 走完整检索链路 → 组装事实 → 生成措辞。"""

    def __init__(
        self,
        retriever: KnowledgeRetriever,
        tool_manager: ToolManager,
        llm: Optional[LLMClient] = None,
    ) -> None:
        self._retriever = retriever
        self._tools = tool_manager
        self._llm: LLMClient = llm or NullLLMClient()
        self._reason_lookup: Dict[str, KnowledgeDoc] = reason_code_lookup()
        self._register_tool()

    @property
    def retriever(self) -> KnowledgeRetriever:
        """检索器。Agent 与 ``/health`` 都要复用同一份索引，不重复建。"""
        return self._retriever

    @property
    def llm(self) -> LLMClient:
        return self._llm

    # ── 工具注册 ──────────────────────────────────────────────────────────────

    def _register_tool(self) -> None:
        """把检索器包装成工具，交给 ToolManager 统一管熔断、缓存、超时。"""

        def handler(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
            query = str(params.get("query", ""))
            top_k = int(params.get("top_k", DEFAULT_TOP_K))
            ctx = context or {}
            hits = self._retriever.search(
                query,
                top_k=top_k,
                reason_codes=ctx.get("reason_codes") or (),
                doc_type=ctx.get("doc_type"),
            )
            return [hit.to_dict() for hit in hits]

        def fallback(
            params: Dict[str, Any],
            context: Optional[Dict[str, Any]],
            error: str,
        ) -> List[Dict[str, Any]]:
            """检索不可用时，退化为「原因码直接查表」。

            注意这不是返回空 —— 原因码释义本来就存在结构化表里，
            不依赖检索也能给出准确答案。检索只影响「能不能找到额外的规范与案例」。
            """
            logger.warning("检索降级为原因码直查: %s", error)
            codes = (context or {}).get("reason_codes") or []
            results: List[Dict[str, Any]] = []
            for code in codes:
                doc = self._reason_lookup.get(str(code))
                if doc is None:
                    continue
                results.append(
                    {
                        "doc_id": doc.doc_id,
                        "title": doc.title,
                        "category": doc.category,
                        "content": doc.content,
                        "score": 1.0,
                        "matched_reason_codes": [str(code)],
                        "retrieval_channels": ["reason_code_lookup"],
                        "chunk_index": 0,
                    }
                )
            return results

        self._tools.register(
            Tool(
                name=TOOL_NAME,
                description="在审核知识库中检索原因码释义、拍摄规范、审核规则与历史案例",
                handler=handler,
                schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer"},
                    },
                },
                cache_ttl=300.0,
                timeout_s=2.5,
                supports_rerank=True,
                fallback=fallback,
            )
        )

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def explain(self, context: ReviewContext, *, top_k: int = DEFAULT_TOP_K) -> Dict[str, Any]:
        started = time.monotonic()
        trace: List[Dict[str, Any]] = []

        reason_codes = context.all_reason_codes
        query = self._build_query(context)
        trace.append({"step": "query_built", "query": query, "reason_codes": reason_codes})

        result: ToolResult = await self._tools.search_with_rewrite(
            TOOL_NAME,
            query,
            top_k=top_k,
            context={"reason_codes": reason_codes, "doc_type": context.doc_type},
            trace=trace,
        )
        hits = [hit for hit in (result.data or []) if isinstance(hit, dict)]

        reason_details = self._build_reason_details(reason_codes)
        unknown_codes = [item["code"] for item in reason_details if item["known"] is False]

        citations = [self._to_citation(hit) for hit in hits]
        actions = self._build_actions(context, reason_details)

        explanation, generation_engine, generation_prompt = await self._generate(
            context, reason_details, actions, citations
        )
        if generation_prompt:
            trace.append(
                {"step": "generate", "engine": generation_engine, "prompt": generation_prompt}
            )

        confidence = self._confidence(hits, reason_details, unknown_codes, generation_engine)
        latency_ms = (time.monotonic() - started) * 1000

        degraded = (
            generation_engine != "llm"
            or not self._llm.available
            or not result.success
        )

        return {
            "request_id": context.request_id,
            "doc_type": context.doc_type,
            "review_result": context.review_result,
            "question": context.effective_question,
            "explanation": explanation,
            "reason_details": reason_details,
            "actions": actions,
            "citations": citations,
            "unknown_reason_codes": unknown_codes,
            "confidence": confidence,
            "degraded": degraded,
            "engine": {
                "llm": self._llm.name,
                "llm_available": self._llm.available,
                "retrieval": self._retriever.vector_backend_name,
                "generation": generation_engine,
                "rewrite": _trace_value(trace, "rewrite", "strategy", "n/a"),
                "rerank": _trace_value(trace, "rerank", "strategy", "n/a"),
                "recall": "ok" if result.success else "degraded",
            },
            "prompt_versions": {
                "registry": PROMPT_REGISTRY_VERSION,
                "used": collect_labels(trace),
            },
            "latency_ms": round(latency_ms, 1),
            "trace": trace,
            "disclaimer": DISCLAIMER,
        }

    # ── 查询构造 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_query(context: ReviewContext) -> str:
        """把记录上下文压成一条查询。

        刻意把原因码字面量拼进去：检索层对原因码有精确命中加权，
        而原因码本身就是这域最可靠的检索信号。
        """
        parts: List[str] = [context.effective_question]
        doc_label = DOC_TYPE_LABELS.get(context.doc_type, context.doc_type)
        parts.append(f"{doc_label}审核")
        if context.review_result:
            parts.append(f"审核结果{REVIEW_RESULT_LABELS.get(context.review_result, context.review_result)}")
        if context.all_reason_codes:
            parts.append("原因码 " + " ".join(context.all_reason_codes))
        parts.append("原因码含义 处置建议 拍摄规范 审核规则")
        return "。".join(part for part in parts if part)

    # ── 事实层 ────────────────────────────────────────────────────────────────

    def _build_reason_details(self, codes: Sequence[str]) -> List[Dict[str, Any]]:
        """委托给模块级 :func:`build_reason_details`，Agent 走同一份实现。"""
        return build_reason_details(codes, self._reason_lookup)

    # ── 处置建议（跨原因码的专家规则）────────────────────────────────────────

    def _build_actions(
        self,
        context: ReviewContext,
        reason_details: Sequence[Dict[str, Any]],
    ) -> List[str]:
        """委托给模块级 :func:`build_actions`。"""
        return build_actions(context, reason_details)

    # ── 措施层 ────────────────────────────────────────────────────────────────

    async def _generate(
        self,
        context: ReviewContext,
        reason_details: Sequence[Dict[str, Any]],
        actions: Sequence[str],
        citations: Sequence[Dict[str, Any]],
    ) -> tuple[str, str, Optional[str]]:
        """生成解释措辞。返回 ``(文本, 生效引擎, 生效 prompt 版本)``。

        prompt 版本只在真调了模型时才有值 —— 模板降级并不是「用了 prompt」，
        报一个版本号上去会让排查者误以为模型参与了生成。
        """
        if self._llm.available:
            try:
                text = await self._generate_with_llm(context, reason_details, citations)
                if text:
                    return text, "llm", EXPLAIN_GENERATE.label
            except LLMUnavailableError as exc:
                logger.warning("解释生成降级为模板: %s", exc)
        return (
            self._generate_with_template(context, reason_details, actions),
            "template",
            None,
        )

    async def _generate_with_llm(
        self,
        context: ReviewContext,
        reason_details: Sequence[Dict[str, Any]],
        citations: Sequence[Dict[str, Any]],
    ) -> str:
        """让 LLM 只做「组织语言」，事实以传入的条目为准。"""
        doc_label = DOC_TYPE_LABELS.get(context.doc_type, context.doc_type)
        result_label = REVIEW_RESULT_LABELS.get(context.review_result, context.review_result or "未知")
        facts = json.dumps(list(reason_details), ensure_ascii=False, indent=2)
        knowledge = "\n".join(
            f"- {item['title']}：{item['content'][:200]}" for item in citations
        )
        system = EXPLAIN_GENERATE.system
        prompt = EXPLAIN_GENERATE.render(
            question=context.effective_question,
            doc_label=doc_label,
            result_label=result_label,
            quality_result=context.quality_result or "无",
            error_message=context.error_message or "无",
            facts=facts,
            knowledge=knowledge or "（无）",
        )
        return (await self._llm.complete(prompt, system=system, max_tokens=512, temperature=0.2)).strip()

    @staticmethod
    def _generate_with_template(
        context: ReviewContext,
        reason_details: Sequence[Dict[str, Any]],
        actions: Sequence[str],
    ) -> str:
        """委托给模块级 :func:`render_template_explanation`。"""
        return render_template_explanation(context, reason_details, actions)

    # ── 置信度 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _confidence(
        hits: Sequence[Dict[str, Any]],
        reason_details: Sequence[Dict[str, Any]],
        unknown_codes: Sequence[str],
        generation_engine: str,
    ) -> float:
        """一个可解释的启发式置信度，不是概率校准过的分数。

        三个因子：知识覆盖率、检索最高分、生成引擎档次。
        前端展示时应当配合解释文本，不要单独展示这个数字当作准确性背书。
        """
        total = len(reason_details)
        coverage = (total - len(unknown_codes)) / total if total else 1.0

        top_score = 0.0
        for hit in hits:
            try:
                top_score = max(top_score, float(hit.get("score", 0.0)))
            except (TypeError, ValueError):
                continue
        if not hits:
            top_score = 0.0

        confidence = 0.5 * coverage + 0.5 * top_score
        if generation_engine != "llm":
            confidence *= 0.8
        if not hits:
            confidence *= 0.7
        return round(min(1.0, max(0.0, confidence)), 2)

    # ── 引用 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _to_citation(hit: Dict[str, Any]) -> Dict[str, Any]:
        return to_citation(hit)


def _first_sentence(text: str) -> str:
    """取首句，用于模板拼装时不把整段话塞进去。"""
    stripped = text.strip()
    if not stripped:
        return ""
    for separator in ("。", "；", "\n"):
        position = stripped.find(separator)
        if 0 <= position <= 120:
            return stripped[: position + 1].rstrip("。") + "。"
    return stripped[:120] + ("…" if len(stripped) > 120 else "")


def _trace_value(
    trace: Sequence[Dict[str, Any]],
    step: str,
    key: str,
    default: Any,
) -> Any:
    for entry in trace:
        if entry.get("step") == step and key in entry:
            return entry[key]
    return default


def build_explainer(
    *,
    llm: Optional[LLMClient] = None,
    retriever: Optional[KnowledgeRetriever] = None,
    tool_manager: Optional[ToolManager] = None,
    documents: Optional[Sequence[KnowledgeDoc]] = None,
) -> ReviewExplainer:
    """组装一个可用的解释器。默认灌入全部内置语料。"""
    active_llm: LLMClient = llm or NullLLMClient()
    active_retriever = retriever or KnowledgeRetriever(documents or load_documents())
    active_tools = tool_manager or ToolManager(active_llm)
    return ReviewExplainer(active_retriever, active_tools, active_llm)
