"""Tests for the ported tool framework: circuit breaker, cache, timeout, fallback.

These are the mechanisms that keep a flaky AI dependency from leaking into the
review UI, so each one gets an explicit failure-mode test.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from ai_service.llm import NullLLMClient
from ai_service.tool_manager import (
    CircuitBreaker,
    CircuitState,
    Tool,
    ToolManager,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def echo_tool(*, cache_ttl: float = 0.0, timeout_s: float = 3.0, fallback=None) -> Tool:
    return Tool(
        name="echo",
        description="回显查询",
        handler=lambda params, context: [{"doc_id": "d1", "score": 1.0, "query": params["query"]}],
        schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
        cache_ttl=cache_ttl,
        timeout_s=timeout_s,
        supports_rerank=False,
        fallback=fallback,
    )


# ── 注册 ──────────────────────────────────────────────────────────────────────

def test_calling_an_unregistered_tool_fails_gracefully() -> None:
    result = run(ToolManager(NullLLMClient()).call("nope", {}))

    assert result.success is False
    assert "工具不存在" in (result.error or "")


def test_unregister_removes_the_tool() -> None:
    manager = ToolManager(NullLLMClient())
    manager.register(echo_tool())

    assert manager.get_tool("echo") is not None

    manager.unregister("echo")

    assert manager.get_tool("echo") is None


# ── 参数校验 ──────────────────────────────────────────────────────────────────

def test_missing_required_parameter_is_rejected() -> None:
    manager = ToolManager(NullLLMClient())
    manager.register(echo_tool())

    result = run(manager.call("echo", {}))

    assert result.success is False
    assert "缺少必需参数" in (result.error or "")


def test_wrong_parameter_type_is_rejected() -> None:
    manager = ToolManager(NullLLMClient())
    manager.register(echo_tool())

    result = run(manager.call("echo", {"query": 123}))

    assert result.success is False
    assert "类型错误" in (result.error or "")


# ── 缓存 ──────────────────────────────────────────────────────────────────────

def test_cache_serves_the_second_identical_call() -> None:
    manager = ToolManager(NullLLMClient())
    manager.register(echo_tool(cache_ttl=60.0))

    first = run(manager.call("echo", {"query": "模糊"}))
    second = run(manager.call("echo", {"query": "模糊"}))

    assert first.success is True
    assert first.cached is False
    assert second.cached is True
    assert second.data == first.data


def test_cache_is_bypassed_when_disabled() -> None:
    manager = ToolManager(NullLLMClient())
    manager.register(echo_tool(cache_ttl=0.0))

    assert run(manager.call("echo", {"query": "模糊"})).cached is False
    assert run(manager.call("echo", {"query": "模糊"})).cached is False


def test_different_parameters_do_not_share_a_cache_entry() -> None:
    manager = ToolManager(NullLLMClient())
    manager.register(echo_tool(cache_ttl=60.0))

    run(manager.call("echo", {"query": "模糊"}))
    second = run(manager.call("echo", {"query": "反光"}))

    assert second.cached is False


def test_clear_cache_empties_the_cache() -> None:
    manager = ToolManager(NullLLMClient())
    manager.register(echo_tool(cache_ttl=60.0))
    run(manager.call("echo", {"query": "模糊"}))

    assert manager.cache_size == 1

    manager.clear_cache()

    assert manager.cache_size == 0


# ── 超时 ──────────────────────────────────────────────────────────────────────

def test_slow_tool_times_out_instead_of_blocking() -> None:
    def slow_handler(params: dict[str, Any], context: Any) -> list[Any]:
        time.sleep(0.5)
        return []

    manager = ToolManager(NullLLMClient())
    manager.register(
        Tool(
            name="slow",
            description="慢工具",
            handler=slow_handler,
            schema={},
            timeout_s=0.05,
        )
    )

    result = run(manager.call("slow", {}))

    assert result.success is False
    assert result.error == "执行超时"


# ── 降级 ──────────────────────────────────────────────────────────────────────

def test_fallback_is_used_when_the_tool_raises() -> None:
    def broken(params: dict[str, Any], context: Any) -> list[Any]:
        raise RuntimeError("boom")

    def fallback(params: dict[str, Any], context: Any, error: str) -> list[Any]:
        return [{"doc_id": "fallback", "score": 1.0, "reason": error}]

    manager = ToolManager(NullLLMClient())
    manager.register(
        Tool(
            name="broken",
            description="坏了",
            handler=broken,
            schema={},
            fallback=fallback,
        )
    )

    result = run(manager.call("broken", {}))

    assert result.success is True
    assert result.data[0]["doc_id"] == "fallback"
    assert "boom" in (result.error or "")


def test_missing_result_is_reported_when_there_is_no_fallback() -> None:
    manager = ToolManager(NullLLMClient())
    manager.register(
        Tool(
            name="broken",
            description="坏了",
            handler=lambda params, context: 1 / 0,
            schema={},
        )
    )

    result = run(manager.call("broken", {}))

    assert result.success is False
    assert result.data is None


# ── 熔断 ──────────────────────────────────────────────────────────────────────

def test_circuit_breaker_opens_after_the_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_s=3600.0)

    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.allow() is True

    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow() is False


def test_circuit_breaker_half_opens_after_the_recovery_window() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_s=0.0)

    breaker.record_failure()

    assert breaker.allow() is True
    assert breaker.state == CircuitState.HALF_OPEN


def test_circuit_breaker_closes_again_after_a_success() -> None:
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure()

    breaker.record_success()

    assert breaker.state == CircuitState.CLOSED
    assert breaker.fail_count == 0


def test_open_circuit_stops_executing_the_tool_and_uses_the_fallback() -> None:
    calls: list[str] = []

    def failing(params: dict[str, Any], context: Any) -> list[Any]:
        calls.append("called")
        raise RuntimeError("always broken")

    manager = ToolManager(NullLLMClient())
    tool = Tool(
        name="flaky",
        description="一直失败",
        handler=failing,
        schema={},
        fallback=lambda params, context, error: [],
    )
    tool.breaker.threshold = 2
    manager.register(tool)

    for _ in range(2):
        run(manager.call("flaky", {}))
    assert len(calls) == 2

    result = run(manager.call("flaky", {}))

    # 熔断后不再真正执行 handler
    assert result.data == []
    assert len(calls) == 2
    assert any(entry["step"] == "tool_circuit_open" for entry in result.trace)


# ── 改写与重排 ────────────────────────────────────────────────────────────────

def test_rewrite_falls_back_to_rule_expansion_without_an_llm() -> None:
    manager = ToolManager(NullLLMClient())

    queries, strategy = run(manager.rewrite_query("图片模糊"))

    assert strategy == "rule"
    assert queries[0] == "图片模糊"
    assert "image_blur" in queries


def test_consensus_order_prefers_documents_found_by_more_sub_queries() -> None:
    items = [
        {"doc_id": "single", "score": 0.9, "consensus": 0.2},
        {"doc_id": "consensus", "score": 0.8, "consensus": 1.0},
    ]

    ordered = ToolManager._consensus_order(items)

    assert ordered[0]["doc_id"] == "consensus"


def test_search_with_rewrite_returns_empty_when_nothing_matches() -> None:
    manager = ToolManager(NullLLMClient())
    manager.register(echo_tool(cache_ttl=0.0))
    # handler 总是返回一条结果，这里换成一个永远空结果的工具
    manager.register(
        Tool(
            name="empty",
            description="空",
            handler=lambda params, context: [],
            schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
        )
    )

    result = run(manager.search_with_rewrite("empty", "模糊", top_k=3))

    assert result.success is False
    assert result.data == []
    assert "所有子查询均无结果" in (result.error or "")


def test_search_with_rewrite_merges_and_deduplicates_across_sub_queries() -> None:
    def handler(params: dict[str, Any], context: Any) -> list[dict[str, Any]]:
        return [
            {"doc_id": "shared", "title": "共享", "category": "reason_code",
             "content": "x", "score": 0.6, "matched_reason_codes": [], "retrieval_channels": []},
            {"doc_id": "rare", "title": "稀有", "category": "reason_code",
             "content": "y", "score": 0.5, "matched_reason_codes": [], "retrieval_channels": []},
        ]

    manager = ToolManager(NullLLMClient())
    manager.register(
        Tool(
            name="dedup",
            description="去重测试",
            handler=handler,
            schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
            supports_rerank=True,
        )
    )

    result = run(manager.search_with_rewrite("dedup", "模糊", top_k=5))

    assert result.success is True
    assert result.reranked is True
    doc_ids = [item["doc_id"] for item in result.data]
    assert doc_ids == sorted(set(doc_ids)) or len(doc_ids) == len(set(doc_ids))
    assert set(doc_ids) == {"shared", "rare"}
    # 两个子查询都会命中 shared，共识度应为 1.0
    shared = next(item for item in result.data if item["doc_id"] == "shared")
    assert shared["consensus"] == pytest.approx(1.0)


def test_stats_expose_success_rate_and_circuit_state() -> None:
    manager = ToolManager(NullLLMClient())
    manager.register(echo_tool())
    run(manager.call("echo", {"query": "模糊"}))

    stats = manager.get_stats()

    assert stats["echo"]["total"] == 1
    assert stats["echo"]["success_rate"] == 1.0
    assert stats["echo"]["circuit"]["state"] == "closed"
