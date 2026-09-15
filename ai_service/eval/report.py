"""把 golden 集、指标、judge 组装成一份可读报告。

这一层只做编排：取样本 → 跑 Agent → 打分 → 聚合 → 对比 baseline。
所有计算都在 :mod:`ai_service.eval.metrics` 与 :mod:`ai_service.eval.judge` 里，
本模块不塞任何业务规则，方便 CLI 与测试共用同一份逻辑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

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

    @property
    def passed(self) -> bool:
        return not self.alerts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "golden": self.golden,
            "metrics": self.metrics,
            "flat": {key: round(value, 4) for key, value in self.flat.items()},
            "judge_engine": self.judge_engine,
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
) -> EvaluationReport:
    """跑完整个 golden 集，产出四层指标报告。

    默认全离线：``llm`` 为 ``None`` 时 Agent 走确定性路径、judge 走确定性 rubric，
    所以 CI 不需要任何 API key。
    """
    rows: List[Dict[str, Any]] = []
    raw_outcomes: List[Dict[str, Any]] = []
    engines: set[str] = set()

    for sample in golden.samples:
        context = ReviewContext.from_payload(sample.to_review_context())
        outcome = await run_agent_for_context(context, llm=llm, budget=budget)
        raw_outcomes.append(outcome)

        judged = await score_answer(
            str(outcome.get("answer") or ""),
            # 把 actions 一并交给 judge：处置建议是独立字段，不看它的话
            # 「有用性」会低估 —— 答复正文本来就只是摘要
            context={**sample.to_review_context(), "actions": outcome.get("actions") or []},
            facts=outcome.get("reason_details") or [],
            llm=llm,
            prefer_llm=prefer_llm_judge,
        )
        engines.add(judged.engine)
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
