"""审核 Agent 的工具白名单与工具实现。

设计立场
--------
1. **白名单是硬边界。** Agent 只能调用这里注册的工具，名字不在
   :data:`TOOL_WHITELIST` 里的一律拒绝。prompt 里写「你只能用这些工具」是
   软约束，模型可以不听；白名单是代码里的硬约束，不听就执行不了。

2. **工具本身不产生副作用。** 四个工具全是只读或纯计算：
   检索、读记录、算阈值、举手找人。没有写库、没有改判、没有外发。
   这既是安全边界，也让「非法调用被拒绝且无副作用」这条验收标准天然成立 ——
   因为根本没有副作用可产生。改判审核结论属于 P2，必须和 ``llm_override``
   落库一起做。

3. **工具描述即 prompt。** :func:`tool_catalog_text` 把工具清单渲染成文本喂给
   planner，这样加工具时只要改这一处，不会出现「prompt 里提了但没注册」的错位。

4. **降级不用 LLM 也能跑。** 每个工具都带确定性 fallback：
   检索挂了退回原因码直查表、记录取不到返回明确的 not_found、
   质检重算永远不需要外部资源。这样 Agent 的 LLM 不可用路径不依赖任何工具成功。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence

from ai_service.corpus import KnowledgeDoc, reason_code_lookup
from ai_service.retrieval import KnowledgeRetriever
from ai_service.thresholds import DEFAULT_THRESHOLDS, ImageQualityThresholds, audit_quality
from ai_service.tool_manager import Tool, ToolManager

logger = logging.getLogger(__name__)

SEARCH_KNOWLEDGE = "search_knowledge"
GET_REVIEW_RECORD = "get_review_record"
RECOMPUTE_QUALITY = "recompute_quality"
ESCALATE_TO_HUMAN = "escalate_to_human"

TOOL_WHITELIST: frozenset[str] = frozenset(
    {SEARCH_KNOWLEDGE, GET_REVIEW_RECORD, RECOMPUTE_QUALITY, ESCALATE_TO_HUMAN}
)

DEFAULT_SEARCH_TOP_K = 5
DEFAULT_RECORD_FIELDS = (
    "request_id",
    "doc_type",
    "review_result",
    "quality_result",
    "quality_reasons",
    "review_reasons",
    "fields",
    "error_message",
    "ocr_mode",
)


class ReviewRecordSource(Protocol):
    """Agent 读取审核记录的入口。刻意做成协议，方便测试替换。"""

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        """按 request_id 返回记录；不存在或无权访问返回 None。"""


@dataclass(frozen=True)
class ContextRecordSource:
    """只服务本次请求那一条记录的数据源。

    为什么不让 AI 服务直连平台数据库
    --------------------------------
    跨进程直连会同时破坏三件事：服务边界（AI 服务要挂载平台的数据文件）、
    安全边界（AI 进程变成能读全库的新面）、部署边界（两个服务没法独立扩缩）。
    所以这里只暴露平台**主动发过来、且已脱敏**的那一条记录。

    代价是 Agent 无法「顺着记录查历史」，但本阶段本来就只做单轮内多步决策，
    不做跨记录挖掘 —— 这个代价是划算的。
    """

    request_id: str
    record: Mapping[str, Any] = field(default_factory=dict)
    allowed_fields: Sequence[str] = DEFAULT_RECORD_FIELDS

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        # 越权防线：只能取本次请求对应的那一条，换个 id 一律 not found
        if not request_id or request_id != self.request_id:
            return None
        if not self.record:
            return None
        return {
            key: self.record[key]
            for key in self.allowed_fields
            if key in self.record
        }


@dataclass(frozen=True)
class ToolSpec:
    """工具元信息。既用于注册，也用于渲染 planner 的工具清单。"""

    name: str
    description: str
    schema: Dict[str, Any]
    cache_ttl: float = 0.0
    timeout_s: float = 3.0
    supports_rerank: bool = False
    returns: str = ""


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name=SEARCH_KNOWLEDGE,
        description=(
            "在审核知识库中检索原因码释义、拍摄规范、审核规则与历史案例。"
            "不知道某个原因码是什么意思、该给用户什么话术时用它。"
        ),
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
        returns="命中的知识片段列表（含 doc_id、标题、正文、分数）",
    ),
    ToolSpec(
        name=GET_REVIEW_RECORD,
        description=(
            "按 request_id 取本次审核记录的结构化字段（结论、原因码、质检结论、"
            "解析出的字段）。需要确认「记录里到底有什么」时用它，不要凭空猜测。"
        ),
        schema={
            "type": "object",
            "required": ["request_id"],
            "properties": {"request_id": {"type": "string"}},
        },
        timeout_s=1.0,
        returns="记录字段字典；不是本次请求的记录会返回 not_found",
    ),
    ToolSpec(
        name=RECOMPUTE_QUALITY,
        description=(
            "用当前线上阈值重新核对这条记录的影像质量判定，看结论与原因码是否自洽、"
            "有没有阈值漂移。涉及「模糊/过暗/过亮/反光」的判定时建议先跑一次。"
        ),
        schema={"type": "object", "properties": {}},
        timeout_s=1.0,
        returns="核对结果，含 mode（metrics=真重算 / flags=仅自洽性）、阈值明细、差异",
    ),
    ToolSpec(
        name=ESCALATE_TO_HUMAN,
        description=(
            "证据不足以给出可靠结论时，主动请求人工复核。"
            "记录里没有原因码、或出现知识库未收录的原因码时应该用它，不要硬编一个解释。"
        ),
        schema={
            "type": "object",
            "required": ["reason"],
            "properties": {
                "reason": {"type": "string"},
                "missing_evidence": {"type": "array"},
            },
        },
        timeout_s=1.0,
        returns="受理回执；该工具不写库、不改判，只是把「需要人」这件事显式化",
    ),
)


def is_whitelisted(name: str) -> bool:
    return name in TOOL_WHITELIST


def spec_of(name: str) -> Optional[ToolSpec]:
    for spec in TOOL_SPECS:
        if spec.name == name:
            return spec
    return None


def tool_catalog_text() -> str:
    """渲染成给 planner 看的工具清单。"""
    lines: List[str] = []
    for spec in TOOL_SPECS:
        required = spec.schema.get("required") or []
        params = ", ".join(required) if required else "无必填参数"
        lines.append(f"- {spec.name}({params})：{spec.description} 返回：{spec.returns}")
    return "\n".join(lines)


# ── 注册 ──────────────────────────────────────────────────────────────────────

def build_review_tools(
    manager: ToolManager,
    *,
    retriever: KnowledgeRetriever,
    records: ReviewRecordSource,
    reason_lookup: Optional[Mapping[str, KnowledgeDoc]] = None,
    thresholds: ImageQualityThresholds = DEFAULT_THRESHOLDS,
) -> None:
    """把四个工具注册到 ``manager``。可重复调用（同名覆盖）。"""
    lookup = dict(reason_lookup) if reason_lookup is not None else reason_code_lookup()

    manager.register(_search_knowledge_tool(retriever, lookup))
    manager.register(_get_review_record_tool(records))
    manager.register(_recompute_quality_tool())
    manager.register(_escalate_tool())


def _search_knowledge_tool(
    retriever: KnowledgeRetriever,
    reason_lookup: Mapping[str, KnowledgeDoc],
) -> Tool:
    def handler(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ctx = context or {}
        hits = retriever.search(
            str(params.get("query", "")),
            top_k=int(params.get("top_k", DEFAULT_SEARCH_TOP_K)),
            reason_codes=ctx.get("reason_codes") or (),
            doc_type=ctx.get("doc_type"),
        )
        return [hit.to_dict() for hit in hits]

    def fallback(
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]],
        error: str,
    ) -> List[Dict[str, Any]]:
        """检索不可用时退回原因码直查表 —— 释义本来就在结构化表里，不依赖检索。"""
        logger.warning("检索降级为原因码直查: %s", error)
        results: List[Dict[str, Any]] = []
        for code in (context or {}).get("reason_codes") or []:
            doc = reason_lookup.get(str(code))
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

    spec = _spec(SEARCH_KNOWLEDGE)
    return Tool(
        name=spec.name,
        description=spec.description,
        handler=handler,
        schema=spec.schema,
        cache_ttl=spec.cache_ttl,
        timeout_s=spec.timeout_s,
        supports_rerank=spec.supports_rerank,
        fallback=fallback,
    )


def _get_review_record_tool(records: ReviewRecordSource) -> Tool:
    spec = _spec(GET_REVIEW_RECORD)

    def handler(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        record = records.get(str(params.get("request_id", "")))
        if record is None:
            return {
                "found": False,
                "error": "not_found",
                "hint": "只能读取本次请求对应的审核记录；请用上下文里的 request_id。",
            }
        return {"found": True, "record": record}

    return Tool(
        name=spec.name,
        description=spec.description,
        handler=handler,
        schema=spec.schema,
        timeout_s=spec.timeout_s,
    )


def _recompute_quality_tool(thresholds: ImageQualityThresholds = DEFAULT_THRESHOLDS) -> Tool:
    spec = _spec(RECOMPUTE_QUALITY)

    def handler(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        ctx = context or {}
        return audit_quality(
            ctx.get("quality_result"),
            list(ctx.get("quality_reasons") or []),
            metrics=ctx.get("quality_metrics") or None,
            thresholds=thresholds,
        )

    return Tool(
        name=spec.name,
        description=spec.description,
        handler=handler,
        schema=spec.schema,
        timeout_s=spec.timeout_s,
    )


def _escalate_tool() -> Tool:
    spec = _spec(ESCALATE_TO_HUMAN)

    def handler(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        missing = params.get("missing_evidence") or []
        if not isinstance(missing, list):
            missing = [str(missing)]
        return {
            "escalated": True,
            "reason": str(params.get("reason", "")).strip(),
            "missing_evidence": [str(item) for item in missing],
            # 显式声明无副作用，避免读 trace 的人以为这里落了库
            "side_effects": [],
        }

    return Tool(
        name=spec.name,
        description=spec.description,
        handler=handler,
        schema=spec.schema,
        timeout_s=spec.timeout_s,
    )


def _spec(name: str) -> ToolSpec:
    spec = spec_of(name)
    if spec is None:  # pragma: no cover - 注册表与调用点同源，构造期就不可能缺
        raise KeyError(f"未定义的工具体: {name}")
    return spec


__all__ = (
    "DEFAULT_RECORD_FIELDS",
    "ESCALATE_TO_HUMAN",
    "GET_REVIEW_RECORD",
    "RECOMPUTE_QUALITY",
    "SEARCH_KNOWLEDGE",
    "TOOL_SPECS",
    "TOOL_WHITELIST",
    "ContextRecordSource",
    "ReviewRecordSource",
    "ToolSpec",
    "build_review_tools",
    "is_whitelisted",
    "spec_of",
    "tool_catalog_text",
)
