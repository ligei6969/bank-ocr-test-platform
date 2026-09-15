"""LLM-as-Judge：四维 rubric 打分 + 与人工标注的一致性校准。

四维固定为 相关性 / 准确性 / 完整性 / 有用性，0~5 分，rubric 写死在 prompt 里
（版本化在 :data:`ai_service.prompts.JUDGE_RUBRIC`），温度固定 0。

**Judge 本身也会飘，所以必须校准。** 两种偏差来源：

* 系统性偏高：模型倾向给「看起来流畅」的答复打高分；
* 长度偏好：长答复得分普遍更高，与正确性无关。

对策不是「相信 judge」，而是拿一份人工标注去量它：一致率多少、平均绝对误差多大。
本模块提供 :func:`calibrate` 输出这些数字，并且提供**离线确定性 judge**，
让整条链路在没有 API key 时也能跑（否则评测入口就进不了 CI）。

离线 judge 的定位要说清楚
--------------------------
:class:`DeterministicRubricJudge` 不是模型，它按可解释的公式算**与 rubric 同形的信号**：

* 相关性 = 答复是否点到了记录里的原因码
* 准确性 = 答复里的数字是否都能在给定事实里找到（反向抓编造）
* 完整性 = 结论 / 根因层 / 下一步 三要素齐不齐
* 有用性 = 是否给出了可执行动作

它**不能替代模型 judge**，价值在于：让指标管线、回归门禁、报告格式在没有
外部依赖的情况下可测试、可运行。真要对外报分数，用 :class:`LLMJudge` 并跑校准。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ai_service.explain import REVIEW_RESULT_LABELS
from ai_service.llm import LLMClient, LLMUnavailableError, NullLLMClient
from ai_service.prompts import JUDGE_RUBRIC
from ai_service.structured import StructuredOutputError, parse_structured
from pydantic import BaseModel, Field

RUBRIC_DIMENSIONS: tuple[str, ...] = ("relevance", "accuracy", "completeness", "usefulness")
MAX_SCORE = 5.0
AGREEMENT_TOLERANCE = 1.0

DETERMINISTIC_ENGINE = "deterministic-rubric"
#: 只挑「像阈值的数字」：带小数点，或两位以上。
#:
#: 为什么不抓所有数字 —— 早期版本用 ``\d+``，结果模板里那句「共命中 1 个原因码」
#: 的「1」被当成不可追溯数字，把一条完全准确的答复打到 0 分；而另一条碰巧
#: 因为在别处出现过「1」就拿满分。单个数位的数字基本是计数而不是阈值，
#: 拿它判准确性等于在测错东西。
THRESHOLD_PATTERN = re.compile(r"\d+\.\d+|\d{2,}")


class JudgeRubricResponse(BaseModel):
    """判分响应的结构。四维必填且必须落在 0~5，理由可空。"""

    relevance: float = Field(ge=0, le=MAX_SCORE)
    accuracy: float = Field(ge=0, le=MAX_SCORE)
    completeness: float = Field(ge=0, le=MAX_SCORE)
    usefulness: float = Field(ge=0, le=MAX_SCORE)
    rationale: str = ""


REQUIRED_ELEMENTS: tuple[str, ...] = ("verdict", "root_cause", "next_step")

#: 判断「答复里有没有出现某条建议」时的最小共享片段长度。
#: 不做逐字比对：模型会把「让用户重新拍摄，」改写成「让用户重新拍摄时，」，
#: 逐字匹配会把正常改写判成没给建议。
MIN_SHARED_FRAGMENT = 6

FULL_COVERAGE_SCORE = MAX_SCORE
PARTIAL_COVERAGE_SCORE = 3.0
NO_COVERAGE_SCORE = 0.0


@dataclass(frozen=True)
class JudgeScore:
    """一条样本的四维得分。"""

    relevance: float
    accuracy: float
    completeness: float
    usefulness: float
    engine: str = DETERMINISTIC_ENGINE
    rationale: str = ""

    @property
    def average(self) -> float:
        return mean(self.as_dict()[dim] for dim in RUBRIC_DIMENSIONS)

    def as_dict(self) -> Dict[str, float]:
        return {dim: float(getattr(self, dim)) for dim in RUBRIC_DIMENSIONS}

    def clamped(self) -> "JudgeScore":
        """把越界分数夹回 0~5 —— 模型偶尔会给 7 分或 -1 分。"""
        values = {
            dim: min(MAX_SCORE, max(0.0, float(getattr(self, dim))))
            for dim in RUBRIC_DIMENSIONS
        }
        return JudgeScore(engine=self.engine, rationale=self.rationale, **values)

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.as_dict(),
            "average": round(self.average, 3),
            "engine": self.engine,
            "rationale": self.rationale,
        }


# ── 离线确定性 judge ──────────────────────────────────────────────────────────

class DeterministicRubricJudge:
    """按公式算与 rubric 同形的信号。不调用任何模型。"""

    name = DETERMINISTIC_ENGINE
    available = True

    def score(
        self,
        answer: str,
        *,
        context: Mapping[str, Any],
        facts: Sequence[Mapping[str, Any]],
    ) -> JudgeScore:
        text = str(answer or "").strip()
        if not text:
            return JudgeScore(0.0, 0.0, 0.0, 0.0, engine=self.name, rationale="答复为空")

        codes = [
            str(item.get("code"))
            for item in facts
            if isinstance(item, Mapping) and item.get("code")
        ]

        return JudgeScore(
            relevance=self._relevance(text, codes),
            accuracy=self._accuracy(text, facts),
            completeness=self._completeness(text, context),
            usefulness=self._usefulness(text, facts, context.get("actions") or []),
            engine=self.name,
            rationale=(
                "离线确定性评分：相关性=原因码覆盖率，准确性=阈值类数字可追溯率，"
                "完整性=结论/根因/下一步三要素，有用性=处置建议覆盖情况"
            ),
        )

    @staticmethod
    def _relevance(text: str, codes: Sequence[str]) -> float:
        if not codes:
            return MAX_SCORE if text else 0.0
        mentioned = sum(1 for code in codes if code in text)
        return MAX_SCORE * mentioned / len(codes)

    @staticmethod
    def _accuracy(text: str, facts: Sequence[Mapping[str, Any]]) -> float:
        """答复里的**阈值类数字**必须能在给定事实里找到 —— 抓编造阈值。

        没有提到任何阈值时给满分：没有可核对的数字声明，就没有可出错的数字声明。
        这不是放水 —— 编造阈值才是这一类最常见的错误，也正是要抓的东西。
        """
        numbers = THRESHOLD_PATTERN.findall(text)
        if not numbers:
            return MAX_SCORE
        haystack = " ".join(
            str(item.get(key, ""))
            for item in facts
            if isinstance(item, Mapping)
            for key in ("implementation", "meaning", "trigger", "advice")
        )
        traceable = sum(1 for number in numbers if number in haystack)
        return MAX_SCORE * traceable / len(numbers)

    @staticmethod
    def _completeness(text: str, context: Mapping[str, Any]) -> float:
        verdict = str(context.get("review_result") or "")
        label = REVIEW_RESULT_LABELS.get(verdict, verdict)
        elements = {
            "verdict": bool(label) and label in text,
            "root_cause": any(token in text for token in ("层", "根因", "质量", "字段")),
            "next_step": any(token in text for token in ("建议", "处置", "重拍", "核对", "报障")),
        }
        return MAX_SCORE * sum(1 for value in elements.values() if value) / len(REQUIRED_ELEMENTS)

    @staticmethod
    def _usefulness(
        text: str,
        facts: Sequence[Mapping[str, Any]],
        actions: Sequence[str],
    ) -> float:
        """审核员看完能不能直接行动。

        候选锚点 = 事实条目里的处置建议与用户话术 + 跨原因码规则给出的处置建议。
        命中任意一条算「可执行」，命中多条算「覆盖到位」。

        用共享片段匹配而不是包含匹配：模型会把「让用户重新拍摄，」写成
        「让用户重新拍摄时，」，逐字比对会把正常改写判成没给建议 ——
        那是在惩罚改写能力，不是在量有用性。
        """
        anchors: List[str] = [str(action) for action in actions if action]
        for item in facts:
            if not isinstance(item, Mapping):
                continue
            for key in ("advice", "user_message"):
                value = str(item.get(key) or "").strip()
                if value:
                    anchors.append(value)

        anchors = [anchor for anchor in anchors if len(anchor) >= MIN_SHARED_FRAGMENT]
        if not anchors:
            return MAX_SCORE if text else NO_COVERAGE_SCORE

        covered = sum(1 for anchor in anchors if _shares_fragment(text, anchor))
        if covered == 0:
            return NO_COVERAGE_SCORE
        if covered == 1:
            return PARTIAL_COVERAGE_SCORE
        return FULL_COVERAGE_SCORE


# ── 在线 LLM judge ────────────────────────────────────────────────────────────

@dataclass
class LLMJudge:
    """用真实模型按固定 rubric 打分。``temperature=0``，prompt 版本化。"""

    llm: LLMClient
    model_label: str = ""

    @property
    def name(self) -> str:
        return f"llm-judge:{self.model_label or self.llm.name}"

    @property
    def available(self) -> bool:
        return self.llm.available

    async def score(
        self,
        answer: str,
        *,
        context: Mapping[str, Any],
        facts: Sequence[Mapping[str, Any]],
    ) -> JudgeScore:
        prompt = JUDGE_RUBRIC.render(
            question=str(context.get("question") or ""),
            verdict=str(context.get("review_result") or ""),
            reason_codes="、".join(
                str(item.get("code")) for item in facts if isinstance(item, Mapping)
            )
            or "无",
            facts=_facts_block(facts),
            answer=str(answer or ""),
        )
        raw = await self.llm.complete(
            prompt,
            system=JUDGE_RUBRIC.system,
            max_tokens=400,
            temperature=0.0,
        )
        parsed = parse_structured(raw, JudgeRubricResponse)
        return JudgeScore(
            relevance=parsed.relevance,
            accuracy=parsed.accuracy,
            completeness=parsed.completeness,
            usefulness=parsed.usefulness,
            engine=self.name,
            rationale=parsed.rationale,
        ).clamped()


def _shares_fragment(text: str, anchor: str, min_length: int = MIN_SHARED_FRAGMENT) -> bool:
    """两段文本是否共享 ``min_length`` 个以上的连续字符。

    滑动窗口逐个试锚点的子串。锚点都很短（一句话量级），代价可以忽略；
    换来的是对「同义改写」的容忍 —— 而容忍改写正是判有用性的前提。
    """
    if not text or not anchor:
        return False
    if len(anchor) < min_length:
        return anchor in text
    for start in range(len(anchor) - min_length + 1):
        if anchor[start : start + min_length] in text:
            return True
    return False


def _facts_block(facts: Sequence[Mapping[str, Any]]) -> str:
    lines: List[str] = []
    for item in facts:
        if not isinstance(item, Mapping):
            continue
        lines.append(
            f"- {item.get('code')}：{item.get('meaning', '')}；"
            f"触发条件 {item.get('trigger') or '未收录'}；"
            f"处置建议 {item.get('advice') or '未收录'}"
        )
    return "\n".join(lines) or "（无）"


# ── 打分入口 ──────────────────────────────────────────────────────────────────

async def score_answer(
    answer: str,
    *,
    context: Mapping[str, Any],
    facts: Sequence[Mapping[str, Any]],
    llm: Optional[LLMClient] = None,
    prefer_llm: bool = False,
) -> JudgeScore:
    """按配置选择 judge。默认离线，只有 ``prefer_llm`` 且模型可用时才走模型。"""
    client = llm or NullLLMClient()
    if prefer_llm and client.available:
        try:
            return await LLMJudge(llm=client).score(answer, context=context, facts=facts)
        except (LLMUnavailableError, StructuredOutputError):
            # judge 自己挂了不能把整轮评测带崩，退回离线打分并把引擎标出来
            fallback = DeterministicRubricJudge().score(answer, context=context, facts=facts)
            return JudgeScore(
                **fallback.as_dict(),
                engine=f"{fallback.engine}(judge-degraded)",
                rationale="模型 judge 不可用，已退回离线确定性评分",
            )
    return DeterministicRubricJudge().score(answer, context=context, facts=facts)


# ── 校准 ──────────────────────────────────────────────────────────────────────

@dataclass
class CalibrationReport:
    """judge 与人工标注的一致率。**这是 judge 可不可信的唯一依据。**"""

    samples: int
    exact_match_rate: float
    within_one_rate: float
    mean_absolute_error: float
    per_dimension: Dict[str, Dict[str, float]] = field(default_factory=dict)
    engine: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "samples": self.samples,
            "exact_match_rate": round(self.exact_match_rate, 4),
            "within_one_rate": round(self.within_one_rate, 4),
            "mean_absolute_error": round(self.mean_absolute_error, 4),
            "per_dimension": {
                name: {key: round(value, 4) for key, value in stats.items()}
                for name, stats in self.per_dimension.items()
            },
            "engine": self.engine,
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        return (
            f"judge 一致率：完全一致 {self.exact_match_rate * 100:.1f}%，"
            f"±1 分内 {self.within_one_rate * 100:.1f}%，"
            f"平均绝对误差 {self.mean_absolute_error:.3f}（{self.samples} 条样本）"
        )


def calibrate(
    judge_scores: Sequence[JudgeScore],
    human_scores: Sequence[Mapping[str, float]],
    *,
    engine: str = "",
    notes: Optional[Sequence[str]] = None,
) -> CalibrationReport:
    """对比 judge 与人工打分。

    两个一致率指标各有用途：

    * **完全一致**是严格要求，rubric 打分很难达到，通常只有两三成；
    * **±1 分内**是实用指标 —— 评审里「4 分还是 5 分」常常无差别，
      但「1 分还是 4 分」是有区别的。所以 ``within_one_rate`` 才是主指标。

    长度不一致时按较短的那个截断，不猜。
    """
    if len(judge_scores) != len(human_scores):
        raise ValueError(
            f"judge 与人工标注数量不一致：{len(judge_scores)} vs {len(human_scores)}"
        )
    if not judge_scores:
        raise ValueError("没有校准样本")

    exact = 0
    within_one = 0
    total = 0
    absolute_errors: List[float] = []
    per_dimension: Dict[str, List[float]] = {dim: [] for dim in RUBRIC_DIMENSIONS}

    for judged, human in zip(judge_scores, human_scores):
        for dim in RUBRIC_DIMENSIONS:
            j_value = float(judged.as_dict()[dim])
            h_value = float(human.get(dim, 0.0))
            total += 1
            error = abs(j_value - h_value)
            absolute_errors.append(error)
            per_dimension[dim].append(error)
            if error == 0:
                exact += 1
            if error <= AGREEMENT_TOLERANCE:
                within_one += 1

    return CalibrationReport(
        samples=len(judge_scores),
        exact_match_rate=exact / total,
        within_one_rate=within_one / total,
        mean_absolute_error=mean(absolute_errors),
        per_dimension={
            dim: {
                "mean_absolute_error": mean(errors),
                "within_one_rate": sum(1 for e in errors if e <= AGREEMENT_TOLERANCE)
                / len(errors),
            }
            for dim, errors in per_dimension.items()
        },
        engine=engine,
        notes=list(notes or []),
    )


__all__ = (
    "AGREEMENT_TOLERANCE",
    "DETERMINISTIC_ENGINE",
    "MAX_SCORE",
    "RUBRIC_DIMENSIONS",
    "CalibrationReport",
    "DeterministicRubricJudge",
    "JudgeRubricResponse",
    "JudgeScore",
    "LLMJudge",
    "calibrate",
    "score_answer",
)
