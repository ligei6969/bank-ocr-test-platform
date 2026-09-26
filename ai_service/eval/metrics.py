"""四层指标与回归门禁。

四层是这么分的（对应方案文档第九节的表）：

========  ==========================================================
工具层    工具选择的序列对不对、参数对不对、平均走了几步
任务层    该找的原因码找全没有、有没有给出依据、处置建议能不能用
解释层    LLM-as-Judge 四维打分（相关性 / 准确性 / 完整性 / 有用性）
成本层    token 用量与「这个数字是不是 provider 回传的真实值」
回归层    各指标 vs ``baseline.json``，退化超过阈值即告警
========  ==========================================================

关于成本层的语义
----------------
离线跑（``LLM_PROVIDER=none``）不调用模型，成本**恒为 0**，这是真实值不是缺失。
所以 ``cost.*`` 只在 ``--live`` 模式下有信息量；CI 用的是离线基线，
成本指标不会在 CI 里触发告警。把它留在指标里是为了让「真实模型跑一次花了多少」
这件事有地方落，而不是让它进 CI 门禁假装有效。

这里全是**纯函数**：输入是样本与运行结果，输出是数字。
不碰文件、不碰网络、不碰 Agent —— 所以「退化 5% 要告警」这条规则可以被直接单测，
不用真跑一遍评测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from ai_service.eval.golden import GoldenSample

#: 默认退化容忍度。超过这个比例就算回归。
DEFAULT_TOLERANCE = 0.05

#: 比较用的浮点容差。
#: 没有它，「正好退化 5%」会因为 ``(0.95 - 1.0) / 1.0 = -0.05000000000000004``
#: 这种误差被判成「超过 5%」而误报。门禁误报比漏报更伤 ——
#: 误报几次之后，所有人都会习惯性忽略它。
COMPARISON_EPSILON = 1e-9

HIGHER_IS_BETTER = "higher"
LOWER_IS_BETTER = "lower"

#: 每个指标的方向。**新增指标时必须在这里登记** —— 否则它不会参与门禁，
#: 这类「静默漏检」比指标本身算错还危险。
METRIC_DIRECTIONS: Dict[str, str] = {
    "tools.sequence_accuracy": HIGHER_IS_BETTER,
    "tools.sequence_subsequence_accuracy": HIGHER_IS_BETTER,
    "tools.parameter_accuracy": HIGHER_IS_BETTER,
    "tools.avg_steps": LOWER_IS_BETTER,
    "task.reason_code_match_rate": HIGHER_IS_BETTER,
    "task.evidence_rate": HIGHER_IS_BETTER,
    "task.action_rate": HIGHER_IS_BETTER,
    "task.escalation_accuracy": HIGHER_IS_BETTER,
    "task.verdict_accuracy": HIGHER_IS_BETTER,
    "task.degraded_rate": LOWER_IS_BETTER,
    "task.truncated_rate": LOWER_IS_BETTER,
    # 双判：只有「质量」类指标才注册方向。
    #
    # ``llm_override_rate`` **刻意不注册** —— 它不是优化目标。「改得多」既不代表
    # 好也不代表坏，一个见谁都放行的模型能刷出 100%。把它当门禁会让指标被
    # 退化模型利用；「突然飙升」属于异常检测，那是 P3 监控层的事（Z-score），
    # 不是回归门禁的事。不注册的副作用是它不参与门禁 —— 这正是想要的。
    "task.llm_failure_rate": LOWER_IS_BETTER,
    "task.harmful_override_rate": LOWER_IS_BETTER,
    "task.llm_adjudication_accuracy": HIGHER_IS_BETTER,
    "explain.relevance": HIGHER_IS_BETTER,
    "explain.accuracy": HIGHER_IS_BETTER,
    "explain.completeness": HIGHER_IS_BETTER,
    "explain.usefulness": HIGHER_IS_BETTER,
    "cost.avg_tokens": LOWER_IS_BETTER,
    "cost.avg_provider_tokens": LOWER_IS_BETTER,
    "cost.provider_usage_rate": HIGHER_IS_BETTER,
}


@dataclass
class RegressionAlert:
    """一条回归告警。"""

    metric: str
    baseline: float
    current: float
    change: float
    tolerance: float
    direction: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline": round(self.baseline, 4),
            "current": round(self.current, 4),
            "change": round(self.change, 4),
            "change_pct": f"{self.change * 100:+.2f}%",
            "tolerance": self.tolerance,
            "direction": self.direction,
        }

    def describe(self) -> str:
        arrow = "下降" if self.direction == HIGHER_IS_BETTER else "上升"
        return (
            f"{self.metric} {arrow} {abs(self.change) * 100:.2f}%"
            f"（{self.baseline:.4f} → {self.current:.4f}，容忍度 {self.tolerance * 100:.0f}%）"
        )


@dataclass
class SampleOutcome:
    """一条样本的评测输入：期望（来自 golden）+ 实际（来自运行）。"""

    sample: GoldenSample
    outcome: Dict[str, Any]
    judge_scores: Mapping[str, float] = field(default_factory=dict)
    judge_engine: str = ""


def _executed_tools(outcome: Mapping[str, Any]) -> List[str]:
    return [
        str(entry.get("tool"))
        for entry in (outcome.get("trace") or [])
        if isinstance(entry, dict) and entry.get("executed")
    ]


def _tool_calls(outcome: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    return [
        entry
        for entry in (outcome.get("trace") or [])
        if isinstance(entry, dict) and entry.get("executed")
    ]


def _token_usage(outcome: Mapping[str, Any]) -> Dict[str, Any]:
    """取 token 用量明细。

    P1.5 之前的结果里没有 ``token_usage`` 字段，按「没调模型、成本为 0」处理 ——
    那是事实，不是缺数据。
    """
    raw = outcome.get("token_usage")
    if not isinstance(raw, Mapping):
        return {"total": 0, "provider_total": 0, "source": "none"}
    return {
        "total": _as_int(raw.get("total")),
        "provider_total": _as_int(raw.get("provider_total")),
        "source": str(raw.get("source") or "none"),
    }


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


# ── 单样本 ────────────────────────────────────────────────────────────────────

def score_sample(row: SampleOutcome) -> Dict[str, Any]:
    """算一条样本的各层原始分数。"""
    sample = row.sample
    outcome = row.outcome
    tools = _executed_tools(outcome)
    tokens = _token_usage(outcome)

    actual_reasons = set(outcome.get("unknown_reason_codes") or [])
    found_reasons = _reasons_covered(outcome)
    expected_reasons = set(sample.expected_reason_codes)
    expected_verdict = getattr(sample, "expected_verdict", None)

    return {
        "sample_id": sample.sample_id,
        "tools.sequence_match": tuple(tools) == tuple(sample.expected_tools),
        "tools.max_sequence_match": _is_subsequence(sample.expected_tools, tools),
        "tools.parameter_ok": _parameters_ok(sample, outcome),
        "tools.steps": len(tools),
        "task.reason_codes_matched": expected_reasons <= found_reasons,
        "task.missing_reason_codes": sorted(expected_reasons - found_reasons),
        "task.unknown_reason_codes": sorted(actual_reasons),
        "task.has_evidence": bool(outcome.get("citations")),
        "task.evidence_expected": "search_knowledge" in sample.expected_tools,
        "task.has_action": bool(outcome.get("actions")),
        "task.escalated": outcome.get("stop_reason") == "escalated",
        "task.escalation_correct": (outcome.get("stop_reason") == "escalated")
        == bool(sample.expects_escalation),
        "task.degraded": bool(outcome.get("degraded")),
        "task.truncated": bool(outcome.get("truncated")),
        "explain.relevance": row.judge_scores.get("relevance", 0.0),
        "explain.accuracy": row.judge_scores.get("accuracy", 0.0),
        "explain.completeness": row.judge_scores.get("completeness", 0.0),
        "explain.usefulness": row.judge_scores.get("usefulness", 0.0),
        "cost.tokens": tokens["total"],
        "cost.provider_tokens": tokens["provider_total"],
        "cost.usage_reported": tokens["source"] == "provider",
        "cost.ran_llm": tokens["source"] != "none",
        # 决策层：只有带人工结论标注的样本才算。没标注时这两个键全 False，
        # 聚合阶段会把整条指标省掉，而不是给出一个 0
        "task.verdict_expected": bool(expected_verdict),
        "task.verdict_correct": bool(expected_verdict)
        and str(outcome.get("review_result") or "") == str(expected_verdict),
        # 双判（P2.3）。``dual_judge_expected`` 以「确实调用过」为判据，
        # 而不是以 llm_override 为真 —— 后者会把「AI 同意」与「AI 没被调到」
        # 混为一谈，让故障样本进入分母
        "task.dual_judge_expected": bool(outcome.get("llm_invoked")),
        "task.llm_override": bool(outcome.get("llm_override")),
        "task.llm_failed": bool(outcome.get("llm_invoked"))
        and str(outcome.get("llm_fallback_reason") or "") != "",
        # 错误放行：改了判、但人工标注说该复核/拒绝
        "task.llm_override_harmful": bool(outcome.get("llm_override"))
        and bool(expected_verdict)
        and str(outcome.get("llm_decision") or "") != str(expected_verdict),
        # 复核建议与人工标注是否一致。
        #
        # 必须排除降级：复核失败时 llm_decision 会回落成规则结论（review），
        # 若把它算进分母，一个「模型完全不可用」的系统会显示成
        # 「复核正确率＝规则正确率」—— 数字好看但不是模型的成绩。
        "task.llm_decision_matches_human": bool(expected_verdict)
        and bool(outcome.get("llm_invoked"))
        and not outcome.get("llm_fallback_reason")
        and str(outcome.get("llm_decision") or "") == str(expected_verdict),
    }


def _reasons_covered(outcome: Mapping[str, Any]) -> set[str]:
    """实际解释覆盖到的原因码。

    以 ``reason_details`` 为准而不是以入参为准：评测的是「解释里讲到了什么」，
    不是「平台告诉它什么」。入参里有而解释里漏掉的，正是要抓的问题。
    """
    covered: set[str] = set()
    for item in outcome.get("reason_details") or []:
        if isinstance(item, dict) and item.get("code"):
            covered.add(str(item["code"]))
    return covered


def _parameters_ok(sample: GoldenSample, outcome: Mapping[str, Any]) -> bool:
    """工具参数对不对。

    只查两件能客观判定的事：
    1. ``get_review_record`` 的 request_id 必须是本样本的 id（查别人就是越权）；
    2. ``search_knowledge`` 的 query 不能是空的。
    """
    for call in _tool_calls(outcome):
        tool = str(call.get("tool"))
        params = call.get("params") or {}
        if tool == "get_review_record" and params.get("request_id") != sample.sample_id:
            return False
        if tool == "search_knowledge" and not str(params.get("query") or "").strip():
            return False
    return True


def _is_subsequence(expected: Sequence[str], actual: Sequence[str]) -> bool:
    remaining = list(expected)
    for name in actual:
        if remaining and name == remaining[0]:
            remaining.pop(0)
    return not remaining


# ── 聚合 ──────────────────────────────────────────────────────────────────────

def _rate(values: Iterable[Any]) -> float:
    items = list(values)
    if not items:
        return 0.0
    return sum(1 for item in items if item) / len(items)


def _conditional_rate(
    rows: Sequence[Mapping[str, Any]],
    *,
    key: str,
    when: str,
) -> float:
    """只在 ``when`` 为真的样本上算 ``key`` 的比率。

    用于「本来就不该有 X」的指标：把不该有的样本算进分母是惩罚正确行为。
    一个满足条件都没有时返回 0.0 并让调用方看出分母为空（报告里会有 count）。
    """
    relevant = [row for row in rows if row.get(when)]
    if not relevant:
        return 0.0
    return _rate(row.get(key) for row in relevant)


def aggregate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把单样本分数聚合成四层指标。"""
    if not rows:
        return {
            "count": 0,
            "tools": {},
            "task": {},
            "explain": {},
            "cost": {},
        }

    steps = [float(row["tools.steps"]) for row in rows]
    return {
        "count": len(rows),
        "tools": {
            "sequence_accuracy": _rate(row["tools.sequence_match"] for row in rows),
            "sequence_subsequence_accuracy": _rate(row["tools.max_sequence_match"] for row in rows),
            "parameter_accuracy": _rate(row["tools.parameter_ok"] for row in rows),
            "avg_steps": sum(steps) / len(steps),
        },
        "task": _task_layer(rows),
        "explain": {
            "relevance": _mean(row["explain.relevance"] for row in rows),
            "accuracy": _mean(row["explain.accuracy"] for row in rows),
            "completeness": _mean(row["explain.completeness"] for row in rows),
            "usefulness": _mean(row["explain.usefulness"] for row in rows),
        },
        "cost": {
            # 用 .get 而不是 []：调用方（含测试）可能手工拼行，缺键当作 0
            "avg_tokens": _mean(row.get("cost.tokens", 0) for row in rows),
            "avg_provider_tokens": _mean(row.get("cost.provider_tokens", 0) for row in rows),
            # 只在「真的调过模型」的样本上算：离线跑全是 0/0，那没有意义
            "provider_usage_rate": _conditional_rate(
                rows, key="cost.usage_reported", when="cost.ran_llm"
            ),
        },
    }


def _mean(values: Iterable[float]) -> float:
    items = [float(item) for item in values]
    return sum(items) / len(items) if items else 0.0


def _task_layer(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """任务层指标。

    ``verdict_accuracy`` **只在有人工结论标注时才出现**。没有标注时若照常
    输出一个 0，报告里那一行看起来就像「结论正确率 0%」—— 一个比「不可用」
    更糟的误读。指标的出现与否本身就是信息。
    """
    block: Dict[str, Any] = {
        "reason_code_match_rate": _rate(row["task.reason_codes_matched"] for row in rows),
        # 只在「本来就该有引用」的样本上算：没有原因码的记录本来就不该检索，
        # 把它算成「缺引用」等于惩罚正确行为
        "evidence_rate": _conditional_rate(
            rows, key="task.has_evidence", when="task.evidence_expected"
        ),
        "action_rate": _rate(row["task.has_action"] for row in rows),
        "escalation_accuracy": _rate(row["task.escalation_correct"] for row in rows),
        "degraded_rate": _rate(row["task.degraded"] for row in rows),
        "truncated_rate": _rate(row["task.truncated"] for row in rows),
    }

    verdict_rows = [row for row in rows if row.get("task.verdict_expected")]
    if verdict_rows:
        block["verdict_accuracy"] = _rate(
            row.get("task.verdict_correct") for row in verdict_rows
        )

    # ── 双判（P2.3）──────────────────────────────────────────────────────────
    #
    # 分母只算**真的跑过双判**的样本（``dual_judge_expected``）。用 llm_override
    # 的真假当分母会同时收进「AI 同意」与「AI 没被调到」两类，故障样本会稀释
    # 改判率 —— 而且方向是反的：故障越多久改判率越低，看起来像规则阈值的问题。
    dual_rows = [row for row in rows if row.get("task.dual_judge_expected")]
    if dual_rows:
        block["llm_override_rate"] = _rate(
            row.get("task.llm_override") for row in dual_rows
        )
        # 失败率：调用过但没拿到可用结论（不可用/超时/输出非法）
        block["llm_failure_rate"] = _rate(
            row.get("task.llm_failed") for row in dual_rows
        )
        # 错误放行率：改了判、但人工标注认为不该放行。分母是**改判次数**，
        # 不是调用次数 —— 回答的是「AI 改的那些里有多少是错的」
        override_rows = [row for row in dual_rows if row.get("task.llm_override")]
        if override_rows:
            block["harmful_override_rate"] = _rate(
                row.get("task.llm_override_harmful") for row in override_rows
            )
        # 复核正确率：AI 的建议与人工标注是否一致。
        # 分母只算**真拿到模型结论**的样本 —— 降级的那些没有「建议」可言，
        # 把它们算进来等于拿规则的成绩冒充模型的成绩
        answered = [
            row
            for row in dual_rows
            if row.get("task.verdict_expected") and not row.get("task.llm_failed")
        ]
        if answered:
            block["llm_adjudication_accuracy"] = _rate(
                row.get("task.llm_decision_matches_human") for row in answered
            )
    return block


def flatten(aggregate_report: Mapping[str, Any]) -> Dict[str, float]:
    """拍平成 ``{"tools.sequence_accuracy": 0.9, ...}``，用于和 baseline 比对。"""
    flat: Dict[str, float] = {}
    for layer in ("tools", "task", "explain", "cost"):
        for name, value in (aggregate_report.get(layer) or {}).items():
            key = f"{layer}.{name}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                flat[key] = float(value)
    return flat


# ── 回归门禁 ──────────────────────────────────────────────────────────────────

def compare_to_baseline(
    current: Mapping[str, float],
    baseline: Mapping[str, float],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    directions: Mapping[str, str] = METRIC_DIRECTIONS,
) -> List[RegressionAlert]:
    """对比 baseline，返回所有超过容忍度的退化。

    两个刻意的行为：

    1. **只报退化，不报提升。** 提升不需要人看，退化才需要。
    2. **baseline 里有、当前没有的指标，跳过而不是当作 0。** 缺指标通常意味着
       评测配置变了（比如某层没跑），把它当 0 会制造一堆假告警，反而淹没真问题。
       真要暴露「指标消失」，那是评测完整性的问题（``missing_metrics``），
       不该混进回归门禁。

    ``tolerance=0.05`` 表示允许 5% 的相对退化。
    """
    alerts: List[RegressionAlert] = []
    for metric, base_value in baseline.items():
        if metric not in current:
            continue
        direction = directions.get(metric)
        if direction is None:
            # 未登记方向的指标不参与门禁 —— 与其猜方向，不如显式要求登记
            continue
        current_value = float(current[metric])
        base = float(base_value)

        if base == 0.0:
            # 基线为 0 时相对变化没有意义（除零），只在真的变差时告警
            if direction == HIGHER_IS_BETTER and current_value < 0:
                change = -1.0
            else:
                continue
        elif direction == HIGHER_IS_BETTER:
            change = (current_value - base) / abs(base)
            if change >= -tolerance - COMPARISON_EPSILON:
                continue
        else:
            change = (current_value - base) / abs(base)
            if change <= tolerance + COMPARISON_EPSILON:
                continue

        alerts.append(
            RegressionAlert(
                metric=metric,
                baseline=base,
                current=current_value,
                change=change,
                tolerance=tolerance,
                direction=direction,
            )
        )
    return sorted(alerts, key=lambda alert: alert.change)


def missing_metrics(
    current: Mapping[str, float],
    baseline: Mapping[str, float],
) -> List[str]:
    """baseline 里有、这次没算出来的指标。"""
    return sorted(set(baseline) - set(current))


#: 刻意不登记方向的指标：它们是**描述性**的，没有「变好/变坏」之分。
#:
#: 目前只有改判率 —— 「改得多」既不代表好也不代表坏，一个见谁都放行的模型
#: 能刷出 100%。把它当门禁会让指标被退化模型利用；「突然飙升」属于异常检测，
#: 那是监控层（Z-score）的事，不是回归门禁的事。
#:
#: 单独列出来是为了让 ``unregistered_metrics`` 保持有效：那个检查的意图是
#: 「发现忘了登记的指标」，如果把它想成「没登记就是错」就会对本该无方向的
#: 指标持续误报，久了就没人看这条告警了。
DIRECTIONLESS_METRICS = frozenset({"task.llm_override_rate"})


def unregistered_metrics(
    current: Mapping[str, float],
    directions: Mapping[str, str] = METRIC_DIRECTIONS,
) -> List[str]:
    """算出来了但没登记方向的指标 —— 它们不会参与门禁，属于静默漏检。

    ``DIRECTIONLESS_METRICS`` 里的不算漏检：那是**刻意**没有方向的。
    """
    return sorted(set(current) - set(directions) - DIRECTIONLESS_METRICS)


__all__ = (
    "COMPARISON_EPSILON",
    "DEFAULT_TOLERANCE",
    "DIRECTIONLESS_METRICS",
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "METRIC_DIRECTIONS",
    "RegressionAlert",
    "SampleOutcome",
    "aggregate",
    "compare_to_baseline",
    "flatten",
    "missing_metrics",
    "score_sample",
    "unregistered_metrics",
)
