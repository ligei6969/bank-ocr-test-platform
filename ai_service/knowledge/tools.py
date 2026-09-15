"""客服工具白名单与实现。

四个工具，全部只读、无副作用：

===============  ==========================================================
search_faq       检索业务 FAQ / 材料清单 / 办理流程 / 术语
search_policy    检索业务规则与合规口径
lookup_product   查询产品通用信息（**只读固定语料，不接实时系统**）
handoff_to_human 证据不足或越界时转人工
===============  ==========================================================

`lookup_product` 为什么不接实时系统
----------------------------------
与审核 Agent 的 ``get_review_record`` 不直连平台数据库是同一个立场：
**AI 进程不该成为新的数据访问面。** 一旦接了实时产品系统，
这个进程就同时拥有了「面向用户的入口」与「对内系统的出口」，
攻击面从「模型说错话」变成「模型能查到不该查的东西」。

而且产品参数接实时系统的收益是假的：真正会变的是费率与额度，
而那两类恰恰是**不允许通过本渠道给的**。接上去只会让它更快地给出
一个必须加免责声明的答案。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai_service.knowledge import corpus
from ai_service.retrieval import KnowledgeRetriever
from ai_service.tool_manager import Tool, ToolManager

SEARCH_FAQ = "search_faq"
SEARCH_POLICY = "search_policy"
LOOKUP_PRODUCT = "lookup_product"
HANDOFF_TO_HUMAN = "handoff_to_human"

KNOWLEDGE_TOOL_WHITELIST: frozenset[str] = frozenset(
    {SEARCH_FAQ, SEARCH_POLICY, LOOKUP_PRODUCT, HANDOFF_TO_HUMAN}
)

DEFAULT_TOP_K = 4

#: 所有产品文档共有的标签。拿它们去匹配等于不匹配。
_GENERIC_PRODUCT_TAGS: frozenset[str] = frozenset({"产品", "通用", "渠道"})


@dataclass(frozen=True)
class KnowledgeToolSpec:
    """工具元信息：既用于注册，也用于渲染给 planner 的清单。"""

    name: str
    description: str
    schema: Dict[str, Any]
    cache_ttl: float = 0.0
    timeout_s: float = 3.0
    supports_rerank: bool = False
    returns: str = ""


KNOWLEDGE_TOOL_SPECS: tuple[KnowledgeToolSpec, ...] = (
    KnowledgeToolSpec(
        name=SEARCH_FAQ,
        description=(
            "检索常见业务问题：办理流程、所需材料、注意事项、术语解释。"
            "客户问「要带什么材料」「怎么办理」「这个名词什么意思」时用它。"
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
    KnowledgeToolSpec(
        name=SEARCH_POLICY,
        description=(
            "检索业务规则与合规口径：实名制、个人信息保护、反洗钱、"
            "代理办理、投诉渠道。涉及「能不能办」「必须提供什么」的合规边界时用它。"
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
        returns="命中的规则片段列表（含 doc_id、标题、正文、分数）",
    ),
    KnowledgeToolSpec(
        name=LOOKUP_PRODUCT,
        description=(
            "查询产品通用信息（借记卡 / 信用卡 / 手机银行等）的功能范围与"
            "与其它产品的通用差异。"
            "**不含费率、利率、额度**——那些以网点公示与您的协议为准，本工具查不到，"
            "也不会编一个给你。查不到就如实说不确定。"
        ),
        schema={
            "type": "object",
            "required": ["product_name"],
            "properties": {"product_name": {"type": "string"}},
        },
        cache_ttl=300.0,
        timeout_s=1.0,
        returns="产品文档；未收录的产品返回 not_found",
    ),
    KnowledgeToolSpec(
        name=HANDOFF_TO_HUMAN,
        description=(
            "问题超出本渠道范围（涉及个人账户数据、个性化授信建议、审核内部信息），"
            "或知识库里确实没有依据时，交给人工。不要凭印象编一个答案。"
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
        returns="受理回执；不写库、不改变任何状态，只把「需要人」显式化",
    ),
)


def is_whitelisted(name: str) -> bool:
    return name in KNOWLEDGE_TOOL_WHITELIST


def spec_of(name: str) -> Optional[KnowledgeToolSpec]:
    for spec in KNOWLEDGE_TOOL_SPECS:
        if spec.name == name:
            return spec
    return None


def tool_catalog_text() -> str:
    """渲染成给 planner 看的工具清单。"""
    lines: List[str] = []
    for spec in KNOWLEDGE_TOOL_SPECS:
        required = spec.schema.get("required") or []
        params = ", ".join(required) if required else "无必填参数"
        lines.append(f"- {spec.name}({params})：{spec.description} 返回：{spec.returns}")
    return "\n".join(lines)


# ── 检索域 ────────────────────────────────────────────────────────────────────

def faq_retriever() -> KnowledgeRetriever:
    """FAQ 域：常见问题 + 术语。"""
    return KnowledgeRetriever(
        corpus.documents_of(corpus.CATEGORY_FAQ)
        + corpus.documents_of(corpus.CATEGORY_GLOSSARY)
    )


def policy_retriever() -> KnowledgeRetriever:
    """规则域：业务规则 + 合规口径。"""
    return KnowledgeRetriever(
        corpus.documents_of(corpus.CATEGORY_POLICY)
        + corpus.documents_of(corpus.CATEGORY_COMPLIANCE)
    )


# ── 注册 ──────────────────────────────────────────────────────────────────────

def build_knowledge_tools(
    manager: ToolManager,
    *,
    faq: KnowledgeRetriever,
    policy: KnowledgeRetriever,
) -> None:
    """把四个客服工具注册到 ``manager``。可重复调用（同名覆盖）。"""
    manager.register(_search_tool(SEARCH_FAQ, faq))
    manager.register(_search_tool(SEARCH_POLICY, policy))
    manager.register(_lookup_product_tool())
    manager.register(_handoff_tool())


def _search_tool(name: str, retriever: KnowledgeRetriever) -> Tool:
    spec = spec_of(name)
    assert spec is not None  # 注册表里一定有；缺了是编码错误，必须立刻炸

    def handler(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        hits = retriever.search(
            str(params.get("query", "")),
            top_k=int(params.get("top_k", DEFAULT_TOP_K)),
        )
        return [hit.to_dict() for hit in hits]

    return Tool(
        name=spec.name,
        description=spec.description,
        handler=handler,
        schema=spec.schema,
        cache_ttl=spec.cache_ttl,
        timeout_s=spec.timeout_s,
        supports_rerank=spec.supports_rerank,
    )


def _lookup_product_tool() -> Tool:
    spec = spec_of(LOOKUP_PRODUCT)
    assert spec is not None

    def handler(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        wanted = str(params.get("product_name", "")).strip()
        if not wanted:
            return {"error": "missing_product_name"}

        matches = _match_products(wanted)
        if not matches:
            # 如实说「查不到」，而不是拿最相近的产品糊弄过去 ——
            # 后者是幻觉最常见的入口。
            return {
                "error": "not_found",
                "product_name": wanted,
                "message": (
                    "知识库未收录该产品的通用信息。"
                    "不要推测它的费率、利率或额度，改为引导用户咨询官方渠道。"
                ),
            }
        return {
            "query": wanted,
            "count": len(matches),
            "matches": matches,
        }

    return Tool(
        name=spec.name,
        description=spec.description,
        handler=handler,
        schema=spec.schema,
        cache_ttl=spec.cache_ttl,
        timeout_s=spec.timeout_s,
    )


def _match_products(name: str) -> List[Dict[str, Any]]:
    """按产品名匹配产品文档，**返回全部命中**。

    匹配方向是「**问题里提到了哪个产品**」，不是「文档里出现了问题的词」。
    方向搞反会同时犯两类错：把「信用卡和借记卡有什么区别」匹配到任何
    正文里提过「信用卡」的文档（比如手机银行），又因为切词切出
    「借记卡有什么区别」这种长串而漏掉真正的借记卡文档。

    判据用文档自己的 ``tags`` 里**非通用**的那些词（产品名、别名）。
    通用词（产品 / 通用 / 渠道）是所有产品文档共有的，拿来匹配等于不匹配。
    """
    question = name.lower().replace(" ", "")
    matches: List[Dict[str, Any]] = []
    for doc in corpus.documents_of(corpus.CATEGORY_PRODUCT):
        needles = [
            tag.lower()
            for tag in doc.tags
            if tag not in _GENERIC_PRODUCT_TAGS and tag.lower() in question
        ]
        if needles:
            matches.append(
                {
                    "doc_id": doc.doc_id,
                    "title": doc.title,
                    "category": doc.category,
                    "content": doc.content,
                    "matched_on": needles,
                }
            )
    return matches


def _handoff_tool() -> Tool:
    spec = spec_of(HANDOFF_TO_HUMAN)
    assert spec is not None

    def handler(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        missing = params.get("missing_evidence") or []
        if isinstance(missing, str):
            missing = [missing]
        return {
            "escalated": True,
            "reason": str(params.get("reason", "")).strip() or "未说明原因",
            "missing_evidence": [str(item) for item in missing],
            "ticket_hint": "记录问题要点后转人工，请勿在对话中要求客户提供证件号或验证码。",
        }

    return Tool(
        name=spec.name,
        description=spec.description,
        handler=handler,
        schema=spec.schema,
        timeout_s=spec.timeout_s,
    )


__all__ = (
    "DEFAULT_TOP_K",
    "HANDOFF_TO_HUMAN",
    "KNOWLEDGE_TOOL_SPECS",
    "KNOWLEDGE_TOOL_WHITELIST",
    "KnowledgeToolSpec",
    "LOOKUP_PRODUCT",
    "SEARCH_FAQ",
    "SEARCH_POLICY",
    "build_knowledge_tools",
    "faq_retriever",
    "is_whitelisted",
    "policy_retriever",
    "spec_of",
    "tool_catalog_text",
)
