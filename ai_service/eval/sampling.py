"""多次采样的真实模型评测：同一配置跑 N 轮，报告方差而不是单点数字。

为什么需要这个模块
------------------
项目自陈的头号弱点是「真实模型只跑过一轮、只试过一个模型 —— 单次采样
不足以断言稳定性」。单轮跑出的 ``tool_set_accuracy=0.925`` 是一个**点**，
不是**分布**：不知道它是稳定值还是运气，也不知道换个模型还成不成立。

本模块做三件事：

1. **重复采样** —— 同一 golden 集、同一配置跑 ``rounds`` 轮，逐指标给出
   均值 / 标准差 / 极差。两个轮次之间不做任何状态共享，每轮重新构造
   Agent（不留会话、不留缓存），保证采的是「独立样本」。
2. **跨模型对照** —— 同一评测在多个模型配置上各跑一组，A 与 B 的差异
   落在同一份报告里，而不是两份各自孤立的 JSON。
3. **诚实边界** —— 只统计真实发生的调用；样本量 ``n`` 写进每一个数字
   旁边；无法计算的（如单点方差）输出 ``null`` 而不是假装是 0。

**刻意不做的事**：
- 不在本模块里做统计显著性检验。40 条样本 × 3 轮的量级做 t 检验是
  伪精确 —— 方差本身就足够指导「能不能信这个数字」了。
- 不把多轮结果平均后写回 baseline。baseline 记录的是**单轮可复现口径**，
  混进平均值会让回归门禁失去明确含义。
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ai_service.eval.golden import GoldenSet, load_golden_set
from ai_service.eval.report import EvaluationReport, evaluate
from ai_service.llm import LLMClient, build_llm_client


@dataclass
class ModelConfig:
    """一组模型配置。同一配置的多轮采样才可比。"""

    name: str
    provider: str
    model: str
    base_url: str = ""
    timeout_s: float = 60.0

    def env(self, api_key: str) -> Dict[str, str]:
        """构造 ``build_llm_client`` 需要的环境变量。

        key 只在这里流动，绝不落盘 —— 调用方从进程环境拿，本模块不持有。
        """
        payload = {
            "LLM_PROVIDER": self.provider,
            "LLM_API_KEY": api_key,
            "LLM_MODEL": self.model,
            "LLM_TIMEOUT_S": str(self.timeout_s),
        }
        if self.base_url:
            payload["LLM_BASE_URL"] = self.base_url
        return payload


@dataclass
class RoundResult:
    """一轮采样的结果：完整报告 + 逐层扁平指标。"""

    round_index: int
    model_name: str
    flat: Dict[str, float]
    execution: Dict[str, Any]

    @property
    def label(self) -> str:
        return f"{self.model_name}#r{self.round_index}"


@dataclass
class MetricSpread:
    """单个指标在多轮采样下的分布。"""

    name: str
    values: List[float]
    mean: float
    stdev: Optional[float]  # 单轮时为 None，不假装是 0
    min: float
    max: float

    @property
    def range(self) -> float:
        return self.max - self.min

    def to_dict(self) -> Dict[str, Any]:
        return {
            "values": [round(v, 4) for v in self.values],
            "n": len(self.values),
            "mean": round(self.mean, 4),
            "stdev": None if self.stdev is None else round(self.stdev, 4),
            "min": round(self.min, 4),
            "max": round(self.max, 4),
            "range": round(self.range, 4),
        }


@dataclass
class SamplingReport:
    """整个采样实验的输出。"""

    rounds: int
    per_model: Dict[str, List[RoundResult]] = field(default_factory=dict)
    spreads: Dict[str, Dict[str, MetricSpread]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rounds": self.rounds,
            "per_model": {
                name: [r.to_dict() if hasattr(r, "to_dict") else _round_dict(r)
                       for r in results]
                for name, results in self.per_model.items()
            },
            "spreads": {
                model: {k: v.to_dict() for k, v in spread.items()}
                for model, spread in self.spreads.items()
            },
            "notes": self.notes,
        }


def _round_dict(result: RoundResult) -> Dict[str, Any]:
    return {
        "round_index": result.round_index,
        "model_name": result.model_name,
        "flat": {k: round(v, 4) for k, v in result.flat.items()},
        "execution": result.execution,
    }


def compute_spreads(results: List[RoundResult]) -> Dict[str, MetricSpread]:
    """把多轮的扁平指标聚合成逐指标分布。

    只有**每一轮都出现**的指标才进分布 —— 一轮有一轮没有的指标，
    说明两轮跑的不是同一个东西，把它平均掉是在掩盖问题。
    """
    if not results:
        return {}
    common = set(results[0].flat)
    for result in results[1:]:
        common &= set(result.flat)

    spreads: Dict[str, MetricSpread] = {}
    for name in sorted(common):
        values = [result.flat[name] for result in results]
        spreads[name] = MetricSpread(
            name=name,
            values=values,
            mean=statistics.fmean(values),
            # 样本标准差；单轮没有自由度，输出 None 而不是 0
            stdev=(statistics.stdev(values) if len(values) >= 2 else None),
            min=min(values),
            max=max(values),
        )
    return spreads


async def run_sampling(
    golden: GoldenSet,
    config: ModelConfig,
    *,
    api_key: str,
    rounds: int,
    baseline_path: Optional[Path] = None,
    verdict_fn=None,
    dual_judge_fn=None,
    max_steps: int = 6,
) -> List[RoundResult]:
    """对同一配置独立跑 ``rounds`` 轮。

    每轮重新 ``build_llm_client`` 并重新构造 Agent 预算 —— 两个轮次之间
    **不共享任何状态**，否则采到的就不是独立样本，方差会被系统性低估。
    """
    if rounds < 1:
        raise ValueError(f"rounds 必须 >= 1，收到 {rounds}")

    results: List[RoundResult] = []
    for index in range(rounds):
        llm: LLMClient = build_llm_client(config.env(api_key))
        if not llm.available:
            raise RuntimeError(
                f"模型 {config.name} 第 {index + 1} 轮构造失败：客户端不可用。"
                "检查 provider/model/base_url 是否匹配。"
            )

        report: EvaluationReport = await evaluate(
            golden,
            llm=llm,
            prefer_llm_judge=True,
            baseline_path=baseline_path,
            verdict_fn=verdict_fn,
            dual_judge_fn=dual_judge_fn,
        )
        results.append(
            RoundResult(
                round_index=index,
                model_name=config.name,
                flat=dict(report.flat),
                execution=dict(report.execution),
            )
        )
    return results


def build_sampling_report(
    rounds: int,
    per_model: Dict[str, List[RoundResult]],
    *,
    notes: Optional[List[str]] = None,
) -> SamplingReport:
    report = SamplingReport(rounds=rounds, per_model=per_model)
    report.notes.extend(
        [
            f"每个配置独立采样 {rounds} 轮；轮间不共享 Agent 状态。",
            "stdev 为样本标准差；单轮采样无自由度，输出 null 而非 0。",
            "样本量 n 已随每个指标给出；n<3 时均值只作参考，不构成稳定性结论。",
        ]
    )
    if notes:
        report.notes.extend(notes)
    report.spreads = {
        name: compute_spreads(results) for name, results in per_model.items()
    }
    return report
