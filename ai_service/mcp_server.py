"""把审核解释与知识客服能力暴露为本地 MCP 工具。

这个入口复用现有 Agent，不复制业务规则：审核工具仍经过 P1 的白名单、预算和
转人工硬规则，客服工具仍经过 P2 的越界与接地闸门。默认使用 stdio transport，
因此不会额外开放一个无认证的网络端口。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ai_service import __version__
from ai_service.agent import run_agent_for_context
from ai_service.explain import ReviewExplainer, ReviewContext, build_explainer
from ai_service.knowledge.agent import KnowledgeAgent, build_knowledge_agent
from ai_service.knowledge.api import MAX_QUESTION_CHARS
from ai_service.knowledge.session import SessionHistory
from ai_service.llm import build_llm_client
from app.logging_utils import mask_sensitive_data, sanitize_review_fields

DOC_TYPES = frozenset({"bank_card", "id_card"})

# MCP 的客服工具面向调用方，不返回只供服务端审计的 rejected_answer / trace。
# 白名单投影比“生成后再删几个字段”更稳：KnowledgeOutcome 将来新增内部字段时，
# 不会在这里被意外公开。
PUBLIC_KNOWLEDGE_FIELDS = (
    "question",
    "answer",
    "intent",
    "citations",
    "actions",
    "handoff",
    "refused",
    "blocked_by",
    "sanitized",
    "grounding",
    "off_topic_dropped",
    "degraded",
    "truncated",
    "stop_reason",
    "history",
    "engine",
    "disclaimer",
)


def _public_knowledge_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {key: result[key] for key in PUBLIC_KNOWLEDGE_FIELDS if key in result}


def create_mcp_server(
    explainer: Optional[ReviewExplainer] = None,
    knowledge_agent: Optional[KnowledgeAgent] = None,
) -> MCPServer:
    """构造可注入依赖的 MCP server，便于离线测试与宿主内嵌。"""
    active_explainer = explainer or build_explainer(llm=build_llm_client())
    active_knowledge_agent = knowledge_agent or build_knowledge_agent(
        llm=active_explainer.llm
    )
    server = MCPServer(
        "bank-ocr-review",
        title="Bank OCR Review Assistant",
        description="银行卡/身份证审核解释与银行业务知识客服",
        instructions=(
            "review_explain 只接收审核上下文并自动脱敏；"
            "knowledge_ask 只回答公开业务知识，越界问题会拒答或转人工。"
        ),
        version=__version__,
    )

    @server.tool(
        name="review_explain",
        title="解释审核结论",
        description="解释一条银行卡或身份证审核记录；输入字段会在进入 Agent 前强制脱敏。",
        structured_output=True,
    )
    async def review_explain(
        request_id: str,
        doc_type: str,
        review_result: str,
        quality_result: Optional[str] = None,
        quality_reasons: Optional[List[str]] = None,
        review_reasons: Optional[List[str]] = None,
        fields: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        ocr_mode: Optional[str] = None,
        question: str = "",
    ) -> Dict[str, Any]:
        """解释审核结论并给出证据、处置建议与是否应转人工。"""
        if doc_type not in DOC_TYPES:
            raise ToolError("doc_type 必须是 bank_card 或 id_card")
        context = ReviewContext.from_payload(
            {
                "request_id": request_id,
                "doc_type": doc_type,
                "review_result": review_result,
                "quality_result": quality_result,
                "quality_reasons": list(quality_reasons or ()),
                "review_reasons": list(review_reasons or ()),
                "fields": sanitize_review_fields(fields or {}),
                "error_message": (
                    mask_sensitive_data(error_message) if error_message else None
                ),
                "ocr_mode": ocr_mode,
                "question": mask_sensitive_data(question),
            }
        )
        return await run_agent_for_context(
            context,
            llm=active_explainer.llm,
            retriever=active_explainer.retriever,
        )

    @server.tool(
        name="knowledge_ask",
        title="咨询银行业务知识",
        description="回答公开银行业务问题；支持由调用方携带的无状态多轮历史。",
        structured_output=True,
    )
    async def knowledge_ask(
        question: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """回答公开业务知识；个人信息、征信与内部操作等越界请求会被拒答。"""
        stripped = question.strip()
        if not stripped:
            raise ToolError("question 不能为空")
        if len(stripped) > MAX_QUESTION_CHARS:
            raise ToolError(f"question 不能超过 {MAX_QUESTION_CHARS} 个字符")
        session = SessionHistory.from_payload(history)
        result = (await active_knowledge_agent.ask(stripped, history=session)).to_dict()
        return _public_knowledge_result(result)

    return server


mcp = create_mcp_server()


def main() -> None:
    """以 stdio 启动；stdout 仅用于 MCP 协议帧。"""
    mcp.run()


if __name__ == "__main__":
    main()


__all__ = (
    "DOC_TYPES",
    "PUBLIC_KNOWLEDGE_FIELDS",
    "create_mcp_server",
    "main",
    "mcp",
)
