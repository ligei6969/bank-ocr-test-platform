"""把 golden 集、指标、judge 组装成一份可读报告。

这一层只做编排：取样本 → 跑 Agent → 打分 → 聚合 → 对比 baseline。
所有计算都在 :mod:`ai_service.eval.metrics` 与 :mod:`ai_service.eval.judge` 里，
本模块不塞任何业务规则，方便 CLI 与测试共用同一份逻辑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ai_service.agent import AgentBudget, run_agent_for_context
from ai_service.eval.golden import GoldenSample, GoldenSet, load_golden_set
from ai_service.eval.judge import (
    CalibrationReport,
    JudgeScore,
    calibrate,
    score_answer,
)
from ai_service.eval.metrics import (
    METRIC_DIRECTIONS,
    RegressionAlert,
    SampleOutcome,
    aggregate,
    compare_to_baseline,
    flatten,
    missing_metrics,
    score_sample,
    unregistered_metrics,
)
from ai_service.explain import ReviewContext
from ai_service.llm import LLMClient, NullLLMClient

DEFAULT_BASELINE_PATH = Path("ai_service/eval/baseline.json")
DEFAULT_CALIBRATION_PATH = Path("ai_service/eval/judge_calibration.json")

#: 算一条样本的平台结论：``(review_result, review_reasons, quality)``。
#: 注入而非直接 import，是为了让纯离线路径不必依赖 FastAPI / OpenCV。
VerdictFn = Callable[[GoldenSample], Tuple[str, List[str], Dict[str, Any]]]

#: 跑双判编排：``(sample, 规则结论, 规则原因码, quality) -> (最终结论, 双判字段)``。
#: 同样是注入而非直连，保持离线路径不依赖平台依赖。
DualJudgeFn = Callable[
    [GoldenSample, str, Sequence[str], Mapping[str, Any]],
    Tuple[str, Dict[str, Any]],
]


@dataclass
class EvaluationReport:
    """一次评测的完整结果。"""

    golden: Dict[str, Any]
    metrics: Dict[str, Any]
    flat: Dict[str, float]
    rows: List[Dict[str, Any]] = field(default_factory=list)
    judge_engine: str = ""
    alerts: List[RegressionAlert] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    unregistered: List[str] = field(default_factory=list)
    baseline_path: Optional[str] = None
    calibration: Optional[CalibrationReport] = None
    execution: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.alerts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "golden": self.golden,
            "metrics": self.metrics,
            "flat": {key: round(value, 4) for key, value in self.flat.items()},
            "judge_engine": self.judge_engine,
            "execution": self.execution,
            "regression": {
                "baseline_path": self.baseline_path,
                "passed": self.passed,
                "alerts": [alert.to_dict() for alert in self.alerts],
                "missing_metrics": self.missing,
                "unregistered_metrics": self.unregistered,
            },
            "calibration": self.calibration.to_dict() if self.calibration else None,
        }


async def evaluate(
    golden: GoldenSet,
    *,
    llm: Optional[LLMClient] = None,
    budget: Optional[AgentBudget] = None,
    prefer_llm_judge: bool = False,
    baseline_path: Optional[Path] = DEFAULT_BASELINE_PATH,
    tolerance: float = 0.05,
    verdict_fn: Optional[VerdictFn] = None,
    dual_judge_fn: Optional[DualJudgeFn] = None,
) -> EvaluationReport:
    """跑完整个 golden 集，产出四层指标报告。

    默认全离线：``llm`` 为 ``None`` 时 Agent 走确定性路径、judge 走确定性 rubric，
    所以 CI 不需要任何 API key。

    ``verdict_fn`` 给出时，每条样本的 ``review_result`` 由**真实平台规则引擎**
    算出，而不是 ``to_review_context`` 的占位推定 —— 决策层指标那时才真正
    测的是平台。默认 ``None`` 保持纯离线，不引入 FastAPI / OpenCV 依赖。
    """
    rows: List[Dict[str, Any]] = []
    raw_outcomes: List[Dict[str, Any]] = []
    engines: set[str] = set()
    judge_model_samples = 0

    for sample in golden.samples:
        base_payload = sample.to_review_context()
        if verdict_fn is not None:
            verdict, verdict_reasons, quality = verdict_fn(sample)
            dual_judge: Dict[str, Any] = {}
            if dual_judge_fn is not None:
                # 双判：规则结论 → 边界判据 → 受限复核，失败回落规则原判。
                # 跑真实编排而不是只读一个字段，这样「边界判据挑没挑对样本」
                # 与「失败有没有回落」都在被测范围内
                verdict, dual_judge = dual_judge_fn(
                    sample, verdict, verdict_reasons, quality
                )
            base_payload = sample.to_review_context(
                review_result=verdict,
                quality_result=quality.get("quality_result"),
                quality_reasons=quality.get("quality_reasons"),
                quality_metrics=quality.get("quality_metrics"),
                dual_judge=dual_judge,
            )
            base_payload["review_reasons"] = list(verdict_reasons)

        context = ReviewContext.from_payload(base_payload)
        outcome = await run_agent_for_context(context, llm=llm, budget=budget)
        # 双判字段并入 outcome —— 决策层指标从 outcome 读，而 Agent 内部
        # 不认识这些键，所以在这里显式合并，而不是指望它透传
        if base_payload.get("dual_judge"):
            outcome.update(
                {
                    "llm_invoked": base_payload["dual_judge"].get("llm_invoked", False),
                    "llm_override": base_payload["dual_judge"].get("llm_override", False),
                    "llm_decision": base_payload["dual_judge"].get("llm_decision", ""),
                    "llm_fallback_reason": base_payload["dual_judge"].get(
                        "llm_fallback_reason", ""
                    ),
                    "boundary_criteria": base_payload["dual_judge"].get(
                        "boundary_criteria", []
                    ),
                }
            )
        raw_outcomes.append(outcome)

        judged = await score_answer(
            str(outcome.get("answer") or ""),
            # 把 actions 一并交给 judge：处置建议是独立字段，不看它的话
            # 「有用性」会低估 —— 答复正文本来就只是摘要
            context={**base_payload, "actions": outcome.get("actions") or []},
            facts=outcome.get("reason_details") or [],
            llm=llm,
            prefer_llm=prefer_llm_judge,
        )
        engines.add(judged.engine)
        judge_model_samples += judged.engine.startswith("llm-judge:")
        rows.append(score_sample(SampleOutcome(sample=sample, outcome=outcome, judge_scores=judged.as_dict())))

    metrics = aggregate(rows)
    flat = flatten(metrics)

    alerts: List[RegressionAlert] = []
    missing: List[str] = []
    baseline_used: Optional[str] = None
    if baseline_path is not None and Path(baseline_path).is_file():
        baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
        baseline_flat = baseline.get("metrics") or {}
        alerts = compare_to_baseline(flat, baseline_flat, tolerance=tolerance)
        missing = missing_metrics(flat, baseline_flat)
        baseline_used = str(baseline_path)

    model_decisions = sum(
        (outcome.get("engine") or {}).get("decision") == "llm"
        and not outcome.get("degraded", True)
        for outcome in raw_outcomes
    )
    expected_adjudications = sum(bool(o.get("llm_invoked")) for o in raw_outcomes)
    successful_adjudications = sum(
        bool(o.get("llm_invoked")) and not o.get("llm_fallback_reason")
        for o in raw_outcomes
    )
    live_requested = llm is not None
    # Configuration and rubric scores are not evidence of a successful call.
    execution = {
        "mode": "live" if live_requested else "offline",
        "model_client": getattr(llm, "name", "none"),
        "judge_model_samples": judge_model_samples,
        "judge_fallback_samples": len(raw_outcomes) - judge_model_samples if prefer_llm_judge else 0,
        "configured_model_available": bool(llm and llm.available),
        "model_decision_samples": model_decisions,
        "fallback_samples": sum(bool(o.get("degraded", True)) for o in raw_outcomes),
        "adjudication_attempts": expected_adjudications,
        "adjudication_successes": successful_adjudications,
        "real_model_verified": bool(live_requested and model_decisions),
        "live_validation_passed": bool(
            live_requested and model_decisions == len(raw_outcomes) and raw_outcomes
            and successful_adjudications == expected_adjudications
            and (not prefer_llm_judge or judge_model_samples == len(raw_outcomes))
        ),
        "model_quality_qualified": None,
        "scope": "offline_regression_and_fallback" if not live_requested else "live_service_evaluation",
        "quality_note": "回归通过不代表模型质量达标；rubric 与作者占位自评不能代替独立人工验收。",
    }
    return EvaluationReport(
        golden=golden.summary(),
        metrics=metrics,
        flat=flat,
        rows=rows,
        judge_engine=", ".join(sorted(engines)),
        alerts=alerts,
        missing=missing,
        unregistered=unregistered_metrics(flat),
        baseline_path=baseline_used,
        execution=execution,
    )


def save_baseline(report: EvaluationReport, path: Path = DEFAULT_BASELINE_PATH) -> Path:
    """把当前指标写成新的基线。

    刻意把 golden 集的指纹一起写进去：换了 golden 集之后，旧基线就不可比了，
    这事必须显式记下来，否则门禁会拿两套不同口径的数字互相比较。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "metrics": {key: round(value, 4) for key, value in report.flat.items()},
                "golden": report.golden,
                "judge_engine": report.judge_engine,
                "directions": METRIC_DIRECTIONS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


# ── 校准 ──────────────────────────────────────────────────────────────────────

def load_calibration_cases(path: Path = DEFAULT_CALIBRATION_PATH) -> List[Dict[str, Any]]:
    if not Path(path).is_file():
        return []
    return list(json.loads(Path(path).read_text(encoding="utf-8")).get("cases") or [])


async def run_calibration(
    path: Path = DEFAULT_CALIBRATION_PATH,
    *,
    llm: Optional[LLMClient] = None,
    prefer_llm_judge: bool = False,
) -> Optional[CalibrationReport]:
    """跑一遍 judge 校准，输出与人工标注的一致率。"""
    cases = load_calibration_cases(path)
    if not cases:
        return None

    judged: List[JudgeScore] = []
    human: List[Mapping[str, float]] = []
    engines: set[str] = set()

    for case in cases:
        score = await score_answer(
            str(case.get("answer") or ""),
            context=case.get("context") or {},
            facts=case.get("facts") or [],
            llm=llm,
            prefer_llm=prefer_llm_judge,
        )
        engines.add(score.engine)
        judged.append(score)
        human.append(case.get("human") or {})

    return calibrate(
        judged,
        human,
        engine=", ".join(sorted(engines)),
        notes=[
            "标注来源：项目作者自评，属**占位标注**，需替换为真实审核员标注后才具备统计意义。",
            f"样本数 {len(cases)}，样本量偏小，一致率只作趋势参考。",
        ],
    )


__all__ = (
    "DEFAULT_BASELINE_PATH",
    "DEFAULT_CALIBRATION_PATH",
    "EvaluationReport",
    "evaluate",
    "load_calibration_cases",
    "run_calibration",
    "save_baseline",
)
