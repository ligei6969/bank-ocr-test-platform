"""Thread-safe, process-local Prometheus metrics for AI agent requests."""

from __future__ import annotations

import math
import threading
from collections import Counter
from typing import Any, Iterable

from ai_service.knowledge.tools import KNOWLEDGE_TOOL_WHITELIST
from ai_service.tools import TOOL_WHITELIST

LATENCY_BUCKETS_SECONDS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
SURFACES = ("explain", "review_agent", "knowledge", "other")
TOKEN_SOURCES = ("none", "provider", "estimate", "mixed")
STOP_REASONS = (
    "finished", "escalated", "refused", "ungrounded", "max_steps", "max_tokens",
    "max_tool_calls", "repeat_call", "too_many_rejections", "other",
)
KNOWN_TOOLS = TOOL_WHITELIST | KNOWLEDGE_TOOL_WHITELIST


def _label(value: Any, allowed: Iterable[str], fallback: str = "other") -> str:
    value = str(value or "")
    return value if value in allowed else fallback


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class AgentMetrics:
    """Aggregate bounded-cardinality counters and latency histograms."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: Counter[tuple[str, str]] = Counter()
        self._degraded: Counter[str] = Counter()
        self._truncated: Counter[tuple[str, str]] = Counter()
        self._llm_calls: Counter[str] = Counter()
        self._tokens: Counter[tuple[str, str]] = Counter()
        self._latencies = {surface: [0] * (len(LATENCY_BUCKETS_SECONDS) + 1) for surface in SURFACES}
        self._latency_sums: Counter[str] = Counter()
        self._tools: Counter[tuple[str, str]] = Counter()
        self._tool_latency_sums: Counter[str] = Counter()
        self._tool_circuit: dict[tuple[str, str], float] = {}

    def observe(self, surface: str, outcome: dict[str, Any]) -> None:
        surface = _label(surface, SURFACES)
        result = "error" if outcome.get("error") else "success"
        if outcome.get("refused"):
            result = "refused"
        elif outcome.get("handoff") or outcome.get("escalation"):
            result = "handoff"
        with self._lock:
            self._requests[(surface, result)] += 1
            if outcome.get("degraded"):
                self._degraded[surface] += 1
            if outcome.get("truncated"):
                self._truncated[(surface, _label(outcome.get("stop_reason"), STOP_REASONS))] += 1
            latency_seconds = _nonnegative_float(outcome.get("latency_ms")) / 1000.0
            for index, bound in enumerate(LATENCY_BUCKETS_SECONDS):
                if latency_seconds <= bound:
                    self._latencies[surface][index] += 1
            self._latencies[surface][-1] += 1
            self._latency_sums[surface] += latency_seconds
            usage = outcome.get("token_usage") or {}
            source = _label(usage.get("source"), TOKEN_SOURCES, "none")
            self._llm_calls[surface] += _nonnegative_int(usage.get("llm_calls"))
            self._tokens[(surface, source)] += _nonnegative_int(usage.get("total", usage.get("total_tokens")))
            trace = outcome.get("trace") or []
            if isinstance(trace, list):
                for entry in trace:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("executed") or entry.get("step") == "tool_cache_hit":
                        tool = _label(entry.get("tool"), KNOWN_TOOLS)
                        status = "success" if entry.get("ok", True) else "failure"
                        self._tools[(tool, status)] += 1
                        self._tool_latency_sums[tool] += _nonnegative_float(entry.get("latency_ms")) / 1000.0
            tools = outcome.get("tools") or {}
            if isinstance(tools, dict):
                for tool_name, stats in tools.items():
                    if not isinstance(stats, dict):
                        continue
                    tool = _label(tool_name, KNOWN_TOOLS)
                    state = _label((stats.get("circuit") or {}).get("state"), ("closed", "open", "half_open"))
                    self._tool_circuit[(tool, state)] = 1.0
                    for known in ("closed", "open", "half_open"):
                        if known != state:
                            self._tool_circuit[(tool, known)] = 0.0

    def observe_error(self, surface: str, latency_ms: float) -> None:
        self.observe(surface, {"error": True, "latency_ms": latency_ms, "token_usage": {"source": "none"}, "trace": [], "tools": {}})

    def render(self) -> str:
        with self._lock:
            lines: list[str] = []
            _counter_family(lines, "bank_ocr_agent_requests_total", "Completed AI requests by surface and outcome.", "counter", self._requests, ("surface", "result"))
            _counter_family(lines, "bank_ocr_agent_degraded_total", "AI requests completed using a degraded path.", "counter", Counter({(s,): n for s, n in self._degraded.items()}), ("surface",))
            _counter_family(lines, "bank_ocr_agent_truncated_total", "AI requests truncated by an agent budget or convergence guard.", "counter", self._truncated, ("surface", "reason"))
            _counter_family(lines, "bank_ocr_agent_llm_calls_total", "LLM calls made while completing AI requests.", "counter", Counter({(s,): n for s, n in self._llm_calls.items()}), ("surface",))
            _counter_family(lines, "bank_ocr_agent_tokens_total", "Reported or estimated tokens by source.", "counter", self._tokens, ("surface", "source"))
            _counter_family(lines, "bank_ocr_tool_calls_total", "Tool calls observed in agent traces.", "counter", self._tools, ("tool", "status"))
            _counter_family(lines, "bank_ocr_tool_latency_seconds_sum", "Cumulative tool latency observed in agent traces.", "counter", Counter({(t,): n for t, n in self._tool_latency_sums.items()}), ("tool",))
            _gauge_family(lines, "bank_ocr_tool_circuit_state", "Current tool circuit state; exactly one state is 1 per observed tool.", self._tool_circuit, ("tool", "state"))
            for index, surface in enumerate(SURFACES):
                _histogram_family(lines, surface, self._latencies[surface], self._latency_sums[surface], include_header=index == 0)
            return "\n".join(lines) + "\n"


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _nonnegative_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) and result >= 0 else 0.0


def _counter_family(lines: list[str], name: str, help_text: str, metric_type: str, values: Counter[tuple[str, ...]], labels: tuple[str, ...]) -> None:
    _family_header(lines, name, help_text, metric_type)
    for label_values, value in sorted(values.items()):
        lines.append(f"{name}{_format_labels(labels, label_values)} {value}")


def _gauge_family(lines: list[str], name: str, help_text: str, values: dict[tuple[str, ...], float], labels: tuple[str, ...]) -> None:
    _family_header(lines, name, help_text, "gauge")
    for label_values, value in sorted(values.items()):
        lines.append(f"{name}{_format_labels(labels, label_values)} {_format_number(value)}")


def _histogram_family(lines: list[str], surface: str, buckets: list[int], total: float, *, include_header: bool) -> None:
    name = "bank_ocr_agent_duration_seconds"
    if include_header:
        _family_header(lines, name, "AI request latency by surface.", "histogram")
    for index, bound in enumerate(LATENCY_BUCKETS_SECONDS):
        lines.append(f'{name}_bucket{{surface="{surface}",le="{bound:g}"}} {buckets[index]}')
    lines.append(f'{name}_bucket{{surface="{surface}",le="+Inf"}} {buckets[-1]}')
    lines.append(f'{name}_sum{{surface="{surface}"}} {_format_number(total)}')
    lines.append(f'{name}_count{{surface="{surface}"}} {buckets[-1]}')


def _family_header(lines: list[str], name: str, help_text: str, metric_type: str) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {metric_type}")


def _format_labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    return "{" + ",".join(f'{name}="{_escape_label(value)}"' for name, value in zip(names, values)) + "}"


def _format_number(value: float) -> str:
    return f"{value:.9g}" if math.isfinite(value) else "0"


__all__ = ("AgentMetrics", "LATENCY_BUCKETS_SECONDS", "SURFACES")
