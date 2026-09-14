"""工具调用框架：完整检索优化链路 + 熔断 + 缓存 + 降级。

移植来源
--------
本模块是 EchoMind ``mcp/tool_manager.py`` 的移植版，保留了它的全部核心设计：

* 查询改写（Query Rewriting）—— 解决「只召回一个角度、召回不全」
* 结果重排（Reranking）     —— 解决「召回不好、排序差」
* 三态熔断器（CLOSED / OPEN / HALF_OPEN）—— 防雪崩
* TTL 结果缓存              —— 减少重复调用与成本
* 超时 + 降级（Fallback）    —— 工具不可用时返回有意义的兜底结果

与 EchoMind 版本的差异（都是有意为之）
------------------------------------
1. **LLM 全程可缺席。** EchoMind 直接 ``AsyncAnthropic``；这里改为注入
   ``LLMClient`` 协议。LLM 不可用时，改写退化为规则同义词扩展、
   重排退化为「共识度」重排，链路不中断、结果仍确定。
2. **去重键改为 doc_id**，而不是整条结果的哈希 —— 语义更准，
   且便于统计「同一条知识被几个子查询命中」。
3. **新增 trace。** 每次调用记录「这一步用的是 LLM 还是降级策略」，
   直接回传给前端与日志，避免降级后被静默吞掉。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from ai_service.llm import LLMClient, LLMUnavailableError, NullLLMClient
from ai_service.retrieval import RetrievalHit, expand_query

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL_S = 300.0
DEFAULT_TOOL_TIMEOUT_S = 3.0
MAX_CACHE_ENTRIES = 2000
CONSENSUS_WEIGHT = 0.4
SCORE_WEIGHT = 0.6


# ── 熔断器 ────────────────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED = "closed"        # 正常
    OPEN = "open"            # 熔断，直接拒绝
    HALF_OPEN = "half_open"  # 探测恢复


class CircuitBreaker:
    """三态熔断器：CLOSED → OPEN → HALF_OPEN → CLOSED。

    与 EchoMind 实现一致：连续失败 ``failure_threshold`` 次后打开，
    打开 ``recovery_s`` 秒后进入 HALF_OPEN 放行一次探测，
    探测成功则关闭，失败则重新打开。
    """

    def __init__(self, failure_threshold: int = 5, recovery_s: float = 60.0) -> None:
        self.threshold = failure_threshold
        self.recovery_s = recovery_s
        self.state = CircuitState.CLOSED
        self.fail_count = 0
        self.opened_at: Optional[float] = None

    def allow(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self.opened_at is not None and time.monotonic() - self.opened_at >= self.recovery_s:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True  # HALF_OPEN：放行一次探测

    def record_success(self) -> None:
        self.fail_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.fail_count += 1
        if self.fail_count >= self.threshold:
            self.state = CircuitState.OPEN
            self.opened_at = time.monotonic()
            logger.warning("工具熔断器打开（连续失败 %s 次）", self.fail_count)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "fail_count": self.fail_count,
            "threshold": self.threshold,
        }


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class ToolStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    total_latency_ms: float = 0.0
    consecutive_fails: int = 0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total else 0.0


@dataclass
class ToolResult:
    success: bool
    data: Any
    tool_name: str
    error: Optional[str] = None
    cached: bool = False
    latency_ms: float = 0.0
    reranked: bool = False
    trace: List[Dict[str, Any]] = field(default_factory=list)


TraceSink = List[Dict[str, Any]]


def _trace(sink: TraceSink, step: str, **payload: Any) -> None:
    """记录一步执行过程，供前端与日志回看。"""
    entry: Dict[str, Any] = {"step": step}
    entry.update(payload)
    sink.append(entry)


@dataclass
class Tool:
    """一个可被调用的工具。字段含义与 EchoMind 版本一致。"""

    name: str
    description: str
    handler: Callable[..., Any]          # sync 或 async，(params, context) -> Any
    schema: Dict[str, Any] = field(default_factory=dict)
    cache_ttl: float = 0.0               # 0 表示不缓存
    timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    supports_rerank: bool = False
    fallback: Optional[Callable[..., Any]] = None

    stats: ToolStats = field(default_factory=ToolStats, init=False)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker, init=False)


# ── 工具管理器 ────────────────────────────────────────────────────────────────

class ToolManager:
    """工具注册、调用与检索优化链路的编排者。"""

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        *,
        cache_ttl: float = DEFAULT_CACHE_TTL_S,
        max_cache_entries: int = MAX_CACHE_ENTRIES,
    ) -> None:
        self._llm: LLMClient = llm or NullLLMClient()
        self._tools: Dict[str, Tool] = {}
        self._cache: Dict[str, tuple[Any, float]] = {}
        self._default_cache_ttl = cache_ttl
        self._max_cache_entries = max_cache_entries

    # ── 注册 ──────────────────────────────────────────────────────────────────

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        logger.info("注册工具: %s", tool.name)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get_tool(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    @property
    def llm(self) -> LLMClient:
        return self._llm

    # ── 核心调用 ──────────────────────────────────────────────────────────────

    async def call(
        self,
        name: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        *,
        use_cache: bool = True,
        rerank_top_k: int = 0,
        trace: Optional[TraceSink] = None,
    ) -> ToolResult:
        """调用工具，执行链：缓存 → 熔断 → 校验 → 执行（含超时）→ 缓存 → 可选重排。"""
        sink: TraceSink = trace if trace is not None else []
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(success=False, data=None, tool_name=name, error=f"工具不存在: {name}")

        if use_cache and tool.cache_ttl > 0:
            cached = self._get_cache(name, params)
            if cached is not None:
                tool.stats.total += 1
                tool.stats.success += 1
                _trace(sink, "tool_cache_hit", tool=name)
                return ToolResult(success=True, data=cached, tool_name=name,
                                  cached=True, trace=sink)

        if not tool.breaker.allow():
            _trace(sink, "tool_circuit_open", tool=name)
            return await self._fallback_result(
                tool, params, context, f"工具熔断中: {name}", sink
            )

        started = time.monotonic()
        tool.stats.total += 1
        try:
            self._validate_params(tool, params)
            data = await self._invoke(tool, params, context)
            latency = (time.monotonic() - started) * 1000

            tool.stats.success += 1
            tool.stats.consecutive_fails = 0
            tool.stats.total_latency_ms += latency
            tool.breaker.record_success()
            _trace(sink, "tool_ok", tool=name, latency_ms=round(latency, 1))

            if tool.cache_ttl > 0:
                self._set_cache(name, params, data, tool.cache_ttl)

            reranked = False
            if rerank_top_k > 0 and tool.supports_rerank and isinstance(data, list):
                query = str(params.get("query", ""))
                data, reranked = await self._rerank(query, data, rerank_top_k, sink), True

            return ToolResult(success=True, data=data, tool_name=name,
                              latency_ms=latency, reranked=reranked, trace=sink)

        except asyncio.TimeoutError:
            tool.stats.failed += 1
            tool.stats.consecutive_fails += 1
            tool.breaker.record_failure()
            _trace(sink, "tool_timeout", tool=name, timeout_s=tool.timeout_s)
            logger.error("工具超时: %s (%ss)", name, tool.timeout_s)
            return await self._fallback_result(tool, params, context, "执行超时", sink)

        except Exception as exc:  # noqa: BLE001 - 工具层必须吞掉所有异常
            tool.stats.failed += 1
            tool.stats.consecutive_fails += 1
            tool.breaker.record_failure()
            _trace(sink, "tool_error", tool=name, error=str(exc))
            logger.error("工具异常: %s — %s", name, exc)
            return await self._fallback_result(tool, params, context, str(exc), sink)

    async def _invoke(
        self,
        tool: Tool,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]],
    ) -> Any:
        """执行工具 handler，并让 ``timeout_s`` 真正生效。

        同步 handler **必须**放到线程里跑。早期实现直接在事件循环线程上调用它，
        结果是超时形同虚设：一个慢 handler 会阻塞整个事件循环，
        ``asyncio.wait_for`` 拿到的只是一个已经算完的值。
        """
        if inspect.iscoroutinefunction(tool.handler):
            return await asyncio.wait_for(
                tool.handler(params, context), timeout=tool.timeout_s
            )

        started = time.monotonic()
        result = await asyncio.wait_for(
            asyncio.to_thread(tool.handler, params, context),
            timeout=tool.timeout_s,
        )
        if inspect.isawaitable(result):
            remaining = max(0.001, tool.timeout_s - (time.monotonic() - started))
            return await asyncio.wait_for(result, timeout=remaining)
        return result

    async def _fallback_result(
        self,
        tool: Tool,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]],
        error: str,
        sink: TraceSink,
    ) -> ToolResult:
        """工具不可用时返回降级结果，而不是把裸错误抛给调用方。"""
        if tool.fallback is None:
            return ToolResult(success=False, data=None, tool_name=tool.name,
                              error=error, trace=sink)
        try:
            data = tool.fallback(params, context, error)
            if inspect.isawaitable(data):
                data = await data
            _trace(sink, "tool_fallback", tool=tool.name, error=error)
            return ToolResult(success=True, data=data, tool_name=tool.name,
                              error=error, trace=sink)
        except Exception as exc:  # noqa: BLE001
            logger.error("工具降级失败: %s — %s", tool.name, exc)
            return ToolResult(success=False, data=None, tool_name=tool.name,
                              error=f"{error}; fallback失败: {exc}", trace=sink)

    # ── 查询改写 ──────────────────────────────────────────────────────────────

    async def rewrite_query(self, query: str, n: int = 3) -> tuple[List[str], str]:
        """把原始查询改写成多个角度的子查询。

        返回 ``(子查询列表, 实际生效的策略)``。策略为 ``llm`` 或 ``rule`` ——
        调用方需要知道到底是哪条路生效了，不能静默降级。
        """
        if not self._llm.available:
            return expand_query(query), "rule"

        prompt = (
            f"将以下审核场景的查询改写为 {n} 个不同角度的检索子查询，用于检索审核知识库。\n"
            "要求：每个子查询角度不同，分别覆盖「原因码含义」「处置建议」「拍摄规范」等不同方面。\n"
            f'原始查询: "{query}"\n'
            '只返回 JSON 数组，例如: ["子查询1", "子查询2", "子查询3"]'
        )
        try:
            raw = await self._llm.complete(prompt, max_tokens=256, temperature=0.3)
            start, end = raw.find("["), raw.rfind("]") + 1
            if start < 0 or end <= start:
                raise ValueError("no JSON array in rewrite response")
            queries = json.loads(raw[start:end])
            if not isinstance(queries, list):
                raise ValueError("rewrite response is not a list")
            cleaned = [str(item).strip() for item in queries if str(item).strip()]
            if not cleaned:
                raise ValueError("empty rewrite result")
            # 原始查询也保留，按出现顺序去重
            return list(dict.fromkeys([query] + cleaned)), "llm"
        except (LLMUnavailableError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("查询改写降级为规则扩展: %s", exc)
            return expand_query(query), "rule"

    # ── 完整检索链路 ──────────────────────────────────────────────────────────

    async def search_with_rewrite(
        self,
        tool_name: str,
        query: str,
        top_k: int = 5,
        context: Optional[Dict[str, Any]] = None,
        *,
        trace: Optional[TraceSink] = None,
    ) -> ToolResult:
        """完整检索优化链路：查询改写 → 并行召回 → 去重合并 → 重排 → Top-K。

        这是整个服务里最核心的一段，也是回答「检索召回不好怎么优化」的完整答案。
        """
        sink: TraceSink = trace if trace is not None else []
        sub_queries, rewrite_strategy = await self.rewrite_query(query, n=3)
        _trace(sink, "rewrite", strategy=rewrite_strategy, sub_queries=sub_queries)

        recall_k = max(top_k, 5)
        tasks = [
            self.call(
                tool_name,
                {"query": sub_query, "top_k": recall_k},
                context,
                use_cache=True,
                trace=[],
            )
            for sub_query in sub_queries
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 合并去重：按 doc_id 归并，同时统计「被几个子查询命中」（共识度）
        merged: Dict[str, Dict[str, Any]] = {}
        for sub_query, result in zip(sub_queries, results):
            if isinstance(result, BaseException) or not isinstance(result, ToolResult):
                continue
            if not result.success or not isinstance(result.data, list):
                continue
            for item in result.data:
                hit = self._coerce_hit(item)
                if hit is None:
                    continue
                bucket = merged.setdefault(
                    hit["doc_id"],
                    {"hit": hit, "score": 0.0, "sources": set(), "reason_codes": set()},
                )
                bucket["score"] = max(float(bucket["score"]), float(hit["score"]))
                bucket["sources"].add(sub_query)
                bucket["reason_codes"].update(hit.get("matched_reason_codes") or [])
                if hit["score"] > bucket["hit"]["score"]:
                    bucket["hit"] = hit

        if not merged:
            _trace(sink, "recall_empty", sub_query_count=len(sub_queries))
            return ToolResult(success=False, data=[], tool_name=tool_name,
                              error="所有子查询均无结果", trace=sink)

        candidates: List[Dict[str, Any]] = []
        for bucket in merged.values():
            hit = dict(bucket["hit"])
            hit["score"] = float(bucket["score"])
            hit["consensus"] = len(bucket["sources"]) / max(1, len(sub_queries))
            hit["matched_reason_codes"] = sorted(bucket["reason_codes"])
            candidates.append(hit)

        _trace(sink, "merge", candidates=len(candidates),
               raw_hits=sum(len(r.data) for r in results
                            if isinstance(r, ToolResult) and isinstance(r.data, list)))

        reranked, rerank_strategy = await self._rerank(query, candidates, top_k, sink)
        _trace(sink, "rerank", strategy=rerank_strategy, kept=len(reranked))

        return ToolResult(
            success=True,
            data=reranked,
            tool_name=tool_name,
            reranked=True,
            trace=sink,
        )

    @staticmethod
    def _coerce_hit(item: Any) -> Optional[Dict[str, Any]]:
        if isinstance(item, RetrievalHit):
            return item.to_dict()
        if isinstance(item, dict) and item.get("doc_id"):
            return dict(item)
        return None

    # ── 结果重排 ──────────────────────────────────────────────────────────────

    async def _rerank(
        self,
        query: str,
        items: List[Dict[str, Any]],
        top_k: int,
        sink: TraceSink,
    ) -> tuple[List[Dict[str, Any]], str]:
        """重排。LLM 可用时用 LLM 打相关性；否则用「共识度」重排。

        降级策略不是摆烂：``consensus`` 表示该条知识被几个不同角度的子查询同时召回，
        被多路同时命中通常意味着它确实切题。这是可解释且确定性的排序信号。
        """
        if len(items) <= top_k:
            return self._consensus_order(items)[:top_k], "consensus"

        if not self._llm.available:
            return self._consensus_order(items)[:top_k], "consensus"

        listing = "\n".join(
            f"{index}. {json.dumps(item, ensure_ascii=False)[:200]}"
            for index, item in enumerate(items)
        )
        prompt = (
            "根据审核员的查询，对以下检索结果按相关性从高到低排序，返回 JSON 索引数组。\n"
            f'查询: "{query}"\n'
            f"检索结果:\n{listing}\n\n"
            "只返回 JSON 数组，例如 [最相关索引, ..., 最不相关索引]，不要任何其他文字。"
        )
        try:
            raw = await self._llm.complete(prompt, max_tokens=256, temperature=0.0)
            start, end = raw.find("["), raw.rfind("]") + 1
            if start < 0 or end <= start:
                raise ValueError("no JSON array in rerank response")
            order = json.loads(raw[start:end])
            if not isinstance(order, list):
                raise ValueError("rerank response is not a list")
            picked = [items[int(i)] for i in order
                      if isinstance(i, (int, float)) and 0 <= int(i) < len(items)]
            seen: set[str] = set()
            deduped: List[Dict[str, Any]] = []
            for item in picked + items:  # 补上 LLM 漏掉的，避免结果缩水
                key = str(item.get("doc_id"))
                if key not in seen:
                    seen.add(key)
                    deduped.append(item)
            if not deduped:
                raise ValueError("empty rerank result")
            return deduped[:top_k], "llm"
        except (LLMUnavailableError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("重排降级为共识度排序: %s", exc)
            _trace(sink, "rerank_fallback", error=str(exc))
            return self._consensus_order(items)[:top_k], "consensus"

    @staticmethod
    def _consensus_order(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def key(item: Dict[str, Any]) -> tuple[float, str]:
            blended = (
                SCORE_WEIGHT * float(item.get("score", 0.0))
                + CONSENSUS_WEIGHT * float(item.get("consensus", 0.0))
            )
            return (-blended, str(item.get("doc_id", "")))

        return sorted(items, key=key)

    # ── 缓存 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(name: str, params: Dict[str, Any]) -> str:
        payload = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        return f"{name}:{hashlib.md5(payload.encode('utf-8')).hexdigest()}"

    def _get_cache(self, name: str, params: Dict[str, Any]) -> Optional[Any]:
        key = self._cache_key(name, params)
        entry = self._cache.get(key)
        if entry is None:
            return None
        data, expire_at = entry
        if time.monotonic() < expire_at:
            return data
        del self._cache[key]
        return None

    def _set_cache(self, name: str, params: Dict[str, Any], data: Any, ttl: float) -> None:
        if len(self._cache) >= self._max_cache_entries:
            # 与 EchoMind 一致：超出上限时先清掉最早写入的四分之一
            for key in list(self._cache)[: self._max_cache_entries // 4]:
                del self._cache[key]
        self._cache[self._cache_key(name, params)] = (data, time.monotonic() + ttl)

    def clear_cache(self) -> None:
        self._cache.clear()

    # ── 参数校验 ──────────────────────────────────────────────────────────────

    _TYPE_MAP: Dict[str, Any] = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    def _validate_params(self, tool: Tool, params: Dict[str, Any]) -> None:
        schema = tool.schema or {}
        for field_name in schema.get("required", []):
            if field_name not in params:
                raise ValueError(f"工具 {tool.name} 缺少必需参数: {field_name}")
        properties = schema.get("properties", {})
        for key, value in params.items():
            expected = properties.get(key, {}).get("type")
            if expected in self._TYPE_MAP and not isinstance(value, self._TYPE_MAP[expected]):
                raise ValueError(
                    f"工具 {tool.name} 参数 {key} 类型错误: "
                    f"期望 {expected}，实际 {type(value).__name__}"
                )

    # ── 统计 ──────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        return {
            name: {
                "total": tool.stats.total,
                "success": tool.stats.success,
                "failed": tool.stats.failed,
                "success_rate": round(tool.stats.success_rate, 3),
                "avg_latency_ms": round(tool.stats.avg_latency_ms, 1),
                "circuit": tool.breaker.snapshot(),
            }
            for name, tool in self._tools.items()
        }

    @property
    def cache_size(self) -> int:
        return len(self._cache)
