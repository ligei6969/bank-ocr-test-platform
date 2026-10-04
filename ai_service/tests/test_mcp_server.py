"""P4b MCP server 的协议级离线验收。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from mcp import Client

from ai_service.mcp_server import create_mcp_server


def _call_tool(name: str, arguments: Dict[str, Any]):
    async def run():
        async with Client(create_mcp_server()) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(run())


def _body(result) -> Dict[str, Any]:
    """SDK v2 用顶层 ``result`` 包装任意 dict 返回值。"""
    return result.structured_content["result"]


def test_mcp_lists_only_the_two_deliberate_capabilities() -> None:
    async def run():
        async with Client(create_mcp_server()) as client:
            return await client.list_tools()

    result = asyncio.run(run())
    assert {tool.name for tool in result.tools} == {"review_explain", "knowledge_ask"}


def test_review_tool_reuses_agent_contract_and_masks_raw_fields() -> None:
    raw_card = "6222021234567890"
    result = _call_tool(
        "review_explain",
        {
            "request_id": "mcp-review-1",
            "doc_type": "bank_card",
            "review_result": "review",
            "quality_result": "review",
            "quality_reasons": ["image_blur"],
            "review_reasons": ["missing_valid_date", "image_blur"],
            "fields": {"card_number": raw_card, "name": "张三"},
            "question": f"卡号 {raw_card} 为什么要复核？",
        },
    )

    assert result.is_error is False
    body = _body(result)
    assert body["request_id"] == "mcp-review-1"
    assert body["stop_reason"] == "finished"
    serialised = json.dumps(body, ensure_ascii=False)
    assert raw_card not in serialised
    assert "张三" not in serialised


def test_knowledge_tool_keeps_policy_gate_and_hides_internal_audit_fields() -> None:
    result = _call_tool("knowledge_ask", {"question": "我征信上有什么问题"})

    assert result.is_error is False
    body = _body(result)
    assert body["refused"] is True
    assert body["intent"] == "pii"
    assert "rejected_answer" not in body
    assert "trace" not in body
    assert "tools" not in body


def test_knowledge_tool_returns_updated_stateless_history() -> None:
    result = _call_tool(
        "knowledge_ask",
        {
            "question": "办理二类账户需要哪些材料",
            "history": [{"question": "我想开二类户", "answer": "请问您想了解什么？"}],
        },
    )

    assert result.is_error is False
    body = _body(result)
    assert body["answer"]
    assert body["history"][-1]["question"] == "办理二类账户需要哪些材料"


def test_review_tool_rejects_an_unknown_document_type() -> None:
    result = _call_tool(
        "review_explain",
        {
            "request_id": "mcp-review-invalid",
            "doc_type": "passport",
            "review_result": "review",
        },
    )

    assert result.is_error is True
    assert "doc_type" in result.content[0].text
