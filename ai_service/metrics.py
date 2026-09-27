"""Thread-safe, process-local Prometheus metrics for AI agent requests."""

from __future__ import annotations

import math
import threading
from collections import Counter
from typing import Any, Iterable

from ai_service.knowledge.tools import KNOWLEDGE_TOOL_WHITELIST
from ai_service.tools import TOOL_WHITELIST

LATENCY_BUCKETS_SECONDS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
SURFACES = ("explain", "review_agent", "adjudicate", "knowledge", "other")
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
        # 双判（P2.3）：改判次数与「没拿到模型结论」的失败次数。
        # 两者必须分开计 —— 只记改判会让「AI 根本没跑成」看起来像「AI 从不改判」。
        self._adjudications: Counter[tuple[str, str]] = Counter()

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
            # 双判：按 (复核结论, 是否降级) 计数。
            #
            # ``llm_override`` 是融合方案点名要的业务指标。这里额外带上
            # ``degraded`` 维度，是因为「改判率 0」有两种截然不同的成因 ——
            # 模型认为该维持 review，或者模型压根没跑成 ——
            # 不分维度就看不出区别，而这两种情况要采取的动作完全相反。
            if surface == "adjudicate":
                decision = _label(
                    outcome.get("decision"), ("review", "pass", "reject", "error")
                )
                fallback = "degraded" if outcome.get("degraded") else "answered"
                self._adjudications[(decision, fallback)] += 1
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

    def _adjudication_counts_locked(self) -> tuple[int, int, int]:
        """返回 ``(总复核数, 改判数(降为 pass), 拿到模型结论的数)``。**调用方须持锁。**

        分母取「拿到模型结论的数」而不是「总复核数」：降级的复核没有「建议」
        可言，把它算进分母会让改判率被故障稀释 —— 而且方向是反的，
        故障越多改判率越低，看起来像规则阈值的问题。这与评测层的口径一致。
        """
        total = sum(self._adjudications.values())
        overrides = self._adjudications.get(("pass", "answered"), 0)
        answered = sum(
            n for (_, result), n in self._adjudications.items() if result == "answered"
        )
        return total, overrides, answered

    def adjudication_counts(self) -> tuple[int, int, int]:
        """线程安全的公开版本，供调用方与告警使用。"""
        with self._lock:
            return self._adjudication_counts_locked()

    def render(self) -> str:
        with self._lock:
            lines: list[str] = []
            _counter_family(lines, "bank_ocr_agent_requests_total", "Completed AI requests by surface and outcome.", "counter", self._requests, ("surface", "result"))
            _counter_family(lines, "bank_ocr_agent_degraded_total", "AI requests completed using a degraded path.", "counter", Counter({(s,): n for s, n in self._degraded.items()}), ("surface",))
            _counter_family(lines, "bank_ocr_agent_truncated_total", "AI requests truncated by an agent budget or convergence guard.", "counter", self._truncated, ("surface", "reason"))
            _counter_family(lines, "bank_ocr_agent_llm_calls_total", "LLM calls made while completing AI requests.", "counter", Counter({(s,): n for s, n in self._llm_calls.items()}), ("surface",))
            _counter_family(lines, "bank_ocr_agent_tokens_total", "Reported or estimated tokens by source.", "counter", self._tokens, ("surface", "source"))
            _counter_family(lines, "bank_ocr_adjudications_total", "Dual-judge outcomes by decision and whether the model answered.", "counter", self._adjudications, ("decision", "result"))
            # 改判率本身也暴露成 gauge —— 融合方案第 198 行点名要这个业务指标。
            # 分母同上：只算拿到模型结论的复核。
            _total, _overrides, _answered = self._adjudication_counts_locked()
            if _answered:
                lines.append("# HELP bank_ocr_adjudication_override_rate AI 改判率（改判为 pass 的复核占比，分母只算拿到模型结论的）。")
                lines.append("# TYPE bank_ocr_adjudication_override_rate gauge")
                lines.append(f"bank_ocr_adjudication_override_rate {_format_number(_overrides / _answered)}")
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


# ── 改判率异常检测（Z-score）────────────────────────────────────────────────────

#: 判定为异常的最小样本量。
#:
#: 「3 个里 1 个」的改判率按 Z-score 算能到 1.0 以上，但那是噪声不是异常。
#: 样本太小时标准差本身就不稳定，报出来的告警只会训练人忽略它。
MIN_ANOMALY_SAMPLES = 20

#: 超过几个标准差算异常。3 是常规做法（正态下约 0.3% 误报）。
ANOMALY_Z_THRESHOLD = 3.0


def override_rate_zscore(recent: int, total: int, *, baseline_rate: float, baseline_samples: int) -> float | None:
    """算改判率的 Z-score；样本不足或基线退化时返回 None。

    为什么要做这件事：融合方案第 202 行点名「改判率突然飙升说明规则阈值漂了
    或者模型变了，要报警」。单看一个绝对值看不出漂移 —— 需要一个相对基线
    的偏离度量，这就是 Z-score 的用途。

    基线本身的方差用二项分布估计：``sqrt(p(1-p)/n)``。这比「拿历史点的标准差」
    更稳，因为后者在样本少时会被单点噪声主导。

    **返回 None 表示「算不出来」，不是「正常」** —— 两者必须区分，
    否则样本不足会被误读成「一切正常」。
    """
    if total < MIN_ANOMALY_SAMPLES or baseline_samples < MIN_ANOMALY_SAMPLES:
        return None
    rate = recent / total
    p = min(max(baseline_rate, 0.0), 1.0)
    std_error = math.sqrt(p * (1.0 - p) / total)
    if std_error <= 0.0:
        # 基线恒为 0 或 1：任何偏离都算异常，但除法无意义，用一个明确的哨兵
        return 0.0 if rate == p else float("inf")
    return (rate - p) / std_error


def override_rate_alert(
    recent: int,
    total: int,
    *,
    baseline_rate: float,
    baseline_samples: int,
    threshold: float = ANOMALY_Z_THRESHOLD,
) -> str | None:
    """改判率异常时返回一句可直接进日志/告警的说明，正常或算不出时返回 None。"""
    zscore = override_rate_zscore(
        recent, total, baseline_rate=baseline_rate, baseline_samples=baseline_samples
    )
    if zscore is None or abs(zscore) < threshold:
        return None
    rate = recent / total
    direction = "飙升" if zscore > 0 else "骤降"
    return (
        f"AI 改判率{direction}：当前 {rate:.1%}（{recent}/{total}），"
        f"基线 {baseline_rate:.1%}，Z={zscore:.2f} 超过阈值 {threshold:g}。"
        "常见成因：规则阈值漂移，或模型/提示词版本变化。"
    )


__all__ = (
    "ANOMALY_Z_THRESHOLD",
    "AgentMetrics",
    "LATENCY_BUCKETS_SECONDS",
    "MIN_ANOMALY_SAMPLES",
    "SURFACES",
    "override_rate_alert",
    "override_rate_zscore",
)
