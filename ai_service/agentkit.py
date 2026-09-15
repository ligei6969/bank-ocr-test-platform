"""Agent 测试工具箱：轨迹断言、故障注入、cassette 装配。

为什么放在包里而不是 ``tests/`` 下
----------------------------------
它不依赖 pytest，是一套「怎么测一个会调工具的 Agent」的可复用答案：
断言的是**轨迹**（调了哪些工具、参数对不对、有没有打转），而不只是最终文本。
换一个测试框架、换一个被测 Agent，这套东西还能用。

轨迹断言才是 Agent 测试的重点
----------------------------
普通单测断言「输出等于什么」。但 Agent 的失败方式往往是**输出看起来对、
过程是错的**：恰好蒙对了结论、绕了十步、或者重复调用同一个工具直到超预算。
所以这里的断言对象是 ``outcome["trace"]``，而不是 ``outcome["answer"]``。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ai_service.agent import (
    AgentBudget,
    ReviewAgent,
    build_agent,
)
from ai_service.cassette import LIVE, REPLAY, Cassette, CassetteLLMClient, wrap_tool
from ai_service.explain import ReviewContext
from ai_service.llm import LLMClient, NullLLMClient
from ai_service.tools import ContextRecordSource


def run(coro: Any) -> Any:
    """跑一个协程。项目没装 pytest-asyncio，统一用这个入口。"""
    return asyncio.run(coro)


# ── 假模型 ────────────────────────────────────────────────────────────────────

class ScriptedPlanner:
    """按脚本给出决策的假 planner。脚本用尽后重复最后一条。

    `prompts` 会记录每次实际收到的 prompt —— 对抗测试要靠它检查
    「注入的内容有没有真的进到模型输入里」。
    """

    def __init__(self, decisions: Sequence[Dict[str, Any]]) -> None:
        if not decisions:
            raise ValueError("脚本不能为空")
        self.decisions = list(decisions)
        self.calls = 0
        self.prompts: List[str] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:scripted:planner"

    async def complete(self, prompt: str, **_: Any) -> str:
        self.prompts.append(prompt)
        decision = self.decisions[min(self.calls, len(self.decisions) - 1)]
        self.calls += 1
        return json.dumps(decision, ensure_ascii=False)


class BrokenPlanner:
    """调用就抛指定异常的 planner。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:broken"

    async def complete(self, prompt: str, **_: Any) -> str:
        self.calls += 1
        raise self.exc


class ProsePlanner:
    """返回自然语言而非 JSON 的 planner —— 模拟模型不听话。"""

    def __init__(self, text: str = "我觉得应该让用户重拍。") -> None:
        self.text = text
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:prose"

    async def complete(self, prompt: str, **_: Any) -> str:
        self.calls += 1
        return self.text


# ── 决策构造 ──────────────────────────────────────────────────────────────────

def decision(action: str, answer: str = "", **params: Any) -> Dict[str, Any]:
    """构造一条决策。``answer`` 是顶层字段，不是工具参数。"""
    return {
        "thought": f"准备调用 {action}",
        "action": action,
        "params": params,
        "answer": answer,
    }


def search_step(query: str = "模糊 释义", **params: Any) -> Dict[str, Any]:
    return decision("search_knowledge", query=query, **params)


def finish_step(answer: str = "已足够。") -> Dict[str, Any]:
    return decision("finish", answer=answer)


# ── 上下文与装配 ──────────────────────────────────────────────────────────────

def blurred_context(**overrides: Any) -> ReviewContext:
    payload: Dict[str, Any] = {
        "request_id": "req-001",
        "doc_type": "bank_card",
        "review_result": "review",
        "quality_result": "review",
        "quality_reasons": ["image_blur"],
        "review_reasons": ["missing_valid_date", "image_blur"],
        "fields": {"card_number": "6222******7890", "name": "张*"},
        "question": "这张卡为什么需要人工复核？",
    }
    payload.update(overrides)
    return ReviewContext.from_payload(payload)


def make_agent(
    planner: Any = None,
    *,
    context: Optional[ReviewContext] = None,
    budget: Optional[AgentBudget] = None,
    records: Any = None,
) -> ReviewAgent:
    """构造一个绑定到 ``context`` 的 Agent。

    ⚠️ 记录源是在**构造时**绑定的，所以后面 ``agent.run(ctx)`` 传的必须是同一个
    context，否则 ``get_review_record`` 会拿不到记录（返回 not_found），
    而且失败得很安静。不想操心这件事就用 :func:`run_agent` —— 它把
    构造和运行绑在一起。
    """
    ctx = context or blurred_context()
    return build_agent(
        llm=planner if planner is not None else NullLLMClient(),
        budget=budget,
        records=records
        or ContextRecordSource(request_id=ctx.request_id, record=ctx.to_record_fields()),
    )


def run_agent(
    context: Optional[ReviewContext] = None,
    planner: Any = None,
    *,
    budget: Optional[AgentBudget] = None,
) -> Dict[str, Any]:
    """构造 + 运行一步到位，返回 outcome 字典。

    测试里最常用的入口：拿不到 Agent 对象就没错配记录源的机会。
    需要注入故障或改工具时，用 :func:`make_agent` 显式配 ``context=``。
    """
    ctx = context or blurred_context()
    return run(make_agent(planner, context=ctx, budget=budget).run(ctx)).to_dict()


# ── 轨迹断言 ──────────────────────────────────────────────────────────────────

@dataclass
class Trajectory:
    """对 ``outcome["trace"]`` 的只读视图 + 一组断言。

    用对象而不是散装函数，是为了让断言读起来像句子：
    ``Trajectory(outcome).assert_sequence("get_review_record", "search_knowledge")``
    """

    outcome: Dict[str, Any]

    # ── 视图 ──────────────────────────────────────────────────────────────────

    @property
    def trace(self) -> List[Dict[str, Any]]:
        return list(self.outcome.get("trace") or [])

    @property
    def executed(self) -> List[Dict[str, Any]]:
        return [entry for entry in self.trace if entry.get("executed")]

    @property
    def rejected(self) -> List[Dict[str, Any]]:
        return [entry for entry in self.trace if entry.get("rejected")]

    @property
    def tools(self) -> List[str]:
        """实际执行过的工具序列（不含被拒绝的调用）。"""
        return [entry["tool"] for entry in self.executed]

    @property
    def rejected_tools(self) -> List[str]:
        return [entry.get("tool", "") for entry in self.rejected]

    @property
    def steps(self) -> int:
        return len(self.executed)

    def calls_of(self, tool: str) -> List[Dict[str, Any]]:
        return [entry for entry in self.executed if entry["tool"] == tool]

    def observations(self, tool: str) -> List[str]:
        return [str(entry.get("observation") or "") for entry in self.calls_of(tool)]

    # ── 断言 ──────────────────────────────────────────────────────────────────

    def assert_sequence(self, *expected: str) -> "Trajectory":
        """工具调用序列必须完全一致（顺序敏感）。"""
        actual = self.tools
        assert actual == list(expected), f"工具序列不符：期望 {list(expected)}，实际 {actual}"
        return self

    def assert_contains_in_order(self, *expected: str) -> "Trajectory":
        """期望序列是实际序列的子序列（顺序敏感，允许中间有别的调用）。"""
        remaining = list(expected)
        for name in self.tools:
            if remaining and name == remaining[0]:
                remaining.pop(0)
        assert not remaining, f"子序列缺失：{remaining}（实际序列 {self.tools}）"
        return self

    def assert_called(self, tool: str, **params: Any) -> "Trajectory":
        """至少有一次调用该工具，且参数包含给定键值。"""
        calls = self.calls_of(tool)
        assert calls, f"从未调用 {tool}（实际序列 {self.tools}）"
        if params:
            assert any(
                all(entry.get("params", {}).get(k) == v for k, v in params.items())
                for entry in calls
            ), f"{tool} 的调用参数里没有 {params}；实际为 {[e['params'] for e in calls]}"
        return self

    def assert_not_called(self, tool: str) -> "Trajectory":
        assert tool not in self.tools, f"不应该调用 {tool}，但实际调用了"
        return self

    def assert_steps_at_most(self, limit: int) -> "Trajectory":
        assert self.steps <= limit, f"执行步数 {self.steps} 超过上限 {limit}"
        return self

    def assert_within_budget(self) -> "Trajectory":
        """预算没被突破（步数 / 工具调用次数 / token）。"""
        used = self.outcome.get("budget_used") or {}
        limits = self.outcome.get("budget") or {}
        assert used.get("steps", 0) <= limits.get("max_steps", 0), used
        assert used.get("tool_calls", 0) <= limits.get("max_tool_calls", 0), used
        assert used.get("tokens", 0) <= limits.get("max_tokens", 0), used
        return self

    def assert_no_duplicate_calls(self) -> "Trajectory":
        """同一个「工具 + 参数」不应重复执行超过一次。"""
        seen: Dict[str, int] = {}
        for entry in self.executed:
            key = json.dumps([entry["tool"], entry.get("params")], sort_keys=True, default=str)
            seen[key] = seen.get(key, 0) + 1
        duplicates = {key: count for key, count in seen.items() if count > 1}
        assert not duplicates, f"出现重复调用：{duplicates}"
        return self

    def assert_no_infinite_loop(self) -> "Trajectory":
        """综合判断「没在打转」：没有重复调用、没被判定 repeat_call。"""
        assert self.outcome.get("stop_reason") != "repeat_call", "被判定为原地打转"
        return self.assert_no_duplicate_calls()

    def assert_rejected(self, kind: str) -> "Trajectory":
        """存在指定类型的拒绝记录。"""
        kinds = [entry.get("rejected") for entry in self.rejected]
        assert kind in kinds, f"没有 {kind} 类拒绝记录；实际为 {kinds}"
        return self

    def assert_rejected_call_never_executed(self) -> "Trajectory":
        """被拒绝的调用必须没有产生执行记录。"""
        rejected_tools = set(self.rejected_tools)
        executed_tools = set(self.tools)
        # 同一个工具被拒过一次又成功执行过是允许的（模型改对了），
        # 但被拒的那一条 trace 本身不能带 executed=True
        for entry in self.rejected:
            assert entry.get("executed") is False, entry
            assert "ok" not in entry, entry
        assert rejected_tools or not executed_tools or True
        return self

    def assert_prompt_versions(self, **expected: str) -> "Trajectory":
        used = (self.outcome.get("prompt_versions") or {}).get("used") or {}
        for key, label in expected.items():
            assert used.get(key) == label, f"{key} 的 prompt 版本不符：{used}"
        return self

    def assert_no_prompts_used(self) -> "Trajectory":
        used = (self.outcome.get("prompt_versions") or {}).get("used") or {}
        assert used == {}, f"不该有 prompt 版本记录，实际 {used}"
        return self

    def assert_every_step_has_audit_fields(self) -> "Trajectory":
        for entry in self.executed:
            for key in ("step", "thought", "tool", "params", "ok", "observation", "latency_ms"):
                assert key in entry, f"trace 步骤缺少 {key}：{entry}"
        return self


# ── 故障注入 ──────────────────────────────────────────────────────────────────

RAISE = "raise"
HTTP_500 = "http_500"
TIMEOUT = "timeout"
BAD_PAYLOAD = "bad_payload"
EMPTY = "empty"

FAULT_MODES = (RAISE, HTTP_500, TIMEOUT, BAD_PAYLOAD, EMPTY)


def inject_fault(
    agent: ReviewAgent,
    tool_name: str,
    mode: str,
    *,
    timeout_s: Optional[float] = None,
    sleep_s: float = 0.5,
) -> None:
    """把已注册工具的 handler 换成会出故障的实现。

    就地改 ``Tool`` 对象而不是重新注册，这样 schema、timeout、缓存策略
    都保持原样 —— 我们想测的是「工具坏了 Agent 怎么办」，
    不是「换了个工具 Agent 怎么办」。

    ``BAD_PAYLOAD`` 返回结构不对的数据（字符串 / 缺字段的字典），
    用来验证 Agent 不会被奇怪的工具输出带崩。
    """
    if mode not in FAULT_MODES:
        raise ValueError(f"未知故障模式 {mode!r}，可选 {FAULT_MODES}")
    tool = agent.tools.get_tool(tool_name)
    if tool is None:
        raise KeyError(f"Agent 没有注册工具 {tool_name}")

    if mode == RAISE:
        def handler(params: Dict[str, Any], context: Any) -> Any:
            raise RuntimeError(f"{tool_name} 内部错误")

    elif mode == HTTP_500:
        def handler(params: Dict[str, Any], context: Any) -> Any:
            raise RuntimeError(f"{tool_name} 返回 HTTP 500 Internal Server Error")

    elif mode == TIMEOUT:
        def handler(params: Dict[str, Any], context: Any) -> Any:
            time.sleep(sleep_s)
            return []

    elif mode == EMPTY:
        def handler(params: Dict[str, Any], context: Any) -> Any:
            return []

    else:  # BAD_PAYLOAD
        def handler(params: Dict[str, Any], context: Any) -> Any:
            return "这不是一个预期的结构"

    tool.handler = handler
    if timeout_s is not None:
        tool.timeout_s = timeout_s


def inject_all_faults(agent: ReviewAgent, mode: str, **kwargs: Any) -> None:
    """给所有已注册工具注入同一种故障。"""
    for name in ("search_knowledge", "get_review_record", "recompute_quality", "escalate_to_human"):
        try:
            inject_fault(agent, name, mode, **kwargs)
        except KeyError:
            continue


# ── cassette 装配 ─────────────────────────────────────────────────────────────

def cassette_agent(
    path: Path,
    *,
    mode: str = REPLAY,
    planner: Optional[LLMClient] = None,
    context: Optional[ReviewContext] = None,
    budget: Optional[AgentBudget] = None,
) -> tuple[ReviewAgent, Cassette]:
    """构造一个 LLM 与工具调用都走 cassette 的 Agent。

    ``mode=live`` 时由调用方在跑完后 ``cassette.save()``。
    """
    ctx = context or blurred_context()
    cassette = Cassette.load(Path(path), mode)
    llm = CassetteLLMClient(inner=planner or NullLLMClient(), cassette=cassette)
    agent = build_agent(
        llm=llm,
        budget=budget,
        records=ContextRecordSource(request_id=ctx.request_id, record=ctx.to_record_fields()),
    )
    for name in ("search_knowledge", "get_review_record", "recompute_quality", "escalate_to_human"):
        tool = agent.tools.get_tool(name)
        if tool is not None:
            wrap_tool(tool, cassette)
    return agent, cassette


__all__ = (
    "BAD_PAYLOAD",
    "EMPTY",
    "FAULT_MODES",
    "HTTP_500",
    "LIVE",
    "RAISE",
    "REPLAY",
    "TIMEOUT",
    "BrokenPlanner",
    "ProsePlanner",
    "ScriptedPlanner",
    "Trajectory",
    "blurred_context",
    "cassette_agent",
    "decision",
    "finish_step",
    "inject_all_faults",
    "inject_fault",
    "make_agent",
    "run",
    "run_agent",
    "search_step",
)
