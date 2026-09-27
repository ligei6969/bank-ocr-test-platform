"""CTE 的数据契约：Event / Prediction / Candidate / Validation 的 schema 与读写。

为什么把 schema 单独成模块
--------------------------
CTE 的每个阶段（Observe → Predict → Compare → Reflect → Candidate → Validate）
都要落地成文件。如果每个阶段各写各的 JSON 形状，三个月后没人知道
哪份文件是权威格式，diff 也就无从谈起。这里把四类记录的形状、
枚举取值、以及「什么算合法」集中定义一次，其余模块只 import。

不可变性的落点
--------------
方案里说「Prediction 保存后不允许覆盖」。这条不是靠文档约束，是靠
:func:`write_prediction` 的 ``FileExistsError`` —— 想改只能追加
``actual_result`` / ``comparison``（见 :class:`Prediction` 的 ``to_dict``），
覆盖写会直接抛错。

不碰什么
--------
本模块**没有任何指向 Ground Truth / Evaluator / 安全不变式的写路径**。
这是方案第十七节「不可自修改区」的落地口径：不是权限隔离（单人仓库里
那只是自欺），而是**正常 CTE 流程里不存在这些资产的合法写入口**。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_EVOLUTION_DIR = ROOT_DIR / "test_evolution"

# ── 枚举取值 ──────────────────────────────────────────────────────────────────

#: 事件来源。方案 Phase 1 列的那些，收敛成可枚举的闭集 ——
#: 自由文本会让统计（「哪一类来源产出最多 Candidate」）没法做。
EVENT_SOURCES: Tuple[str, ...] = (
    "pytest_failure",
    "ci_regression",
    "ocr_error",
    "manual_review",
    "adjudication_error",
    "agent_tool_error",
    "threat_test_failure",
    "production_incident",
    "new_bug",
    "new_requirement",
)

#: 事件落在哪个产品面。**这个是 CTE Readiness 的判据** ——
#: OCR / adjudication 两个面在数据真实化（CTE-2）完成前不允许产出学习结论，
#: 见 :data:`BLOCKED_SURFACES`。
EVENT_SURFACES: Tuple[str, ...] = (
    "knowledge",
    "threat",
    "agent",
    "ocr",
    "adjudication",
)

#: 数据真实性未解决前，这两个面只能记录事件，不能据此产出 Candidate。
#: 理由不是「不能测」——安全不变式照跑 —— 而是**不能从当前数据得出
#: 「双判行为应该怎么改」的学习结论**：fields 是标注真值而非 OCR 实际输出，
#: 「字段全部解析成功」恒成立，信号没有区分度。
BLOCKED_SURFACES: frozenset[str] = frozenset({"ocr", "adjudication"})

#: Candidate 类型。方案第十一节。
CANDIDATE_TYPES: Tuple[str, ...] = (
    "NEW_TEST",
    "NEW_TEST_DATA",
    "FAILURE_PATTERN",
    "BOUNDARY_CASE",
    "SKILL_UPDATE",
    "RISK_RULE",
    "THREAT_CASE",
    "DOCUMENTATION",
)

#: 生命周期。**没有 candidate → validated 的直达路径** ——
#: 中间必须有 machine_validated 与 human_review 两个状态。
CANDIDATE_STATUSES: Tuple[str, ...] = (
    "candidate",
    "machine_validated",
    "rejected",
    "validated",
    "deprecated",
)

#: 验证矩阵（方案第十二节）。每类 Candidate **必须**执行哪些检查。
#: 这张表是 :func:`validate_candidate` 的唯一依据 —— 加类型必须同时加判据，
#: 不允许出现「有类型但不知道该怎么验」的空档。
VALIDATION_MATRIX: Dict[str, Tuple[str, ...]] = {
    "NEW_TEST": ("executable", "reproduces_before_fix", "passes_after_fix", "full_regression"),
    "NEW_TEST_DATA": ("executable", "full_regression"),
    "FAILURE_PATTERN": ("has_evidence", "history_verified"),
    "BOUNDARY_CASE": ("executable", "adjacent_values", "full_regression"),
    "SKILL_UPDATE": ("eval_before_after", "full_regression"),
    "RISK_RULE": ("proposal_only",),
    "THREAT_CASE": ("threat_runner", "expected_policy_verdict", "full_regression"),
    "DOCUMENTATION": ("has_evidence",),
}

#: **每一类都必须在人工批准后才能晋级。** 方案第十一节条件 6 说的是
#: 「不存在 machine_validated → production 的直达路径」—— 它没有例外，
#: 所以这张表不是「哪些类型需要人看」，而是「所有类型」。
#: 之所以单独列出来而不是写死 ``True``：将来若真有一类可以免人工，
#: 改动会落在这一行上，评审时一眼能看见。
TYPES_REQUIRING_HUMAN_APPROVAL: frozenset[str] = frozenset(CANDIDATE_TYPES)


class SchemaError(ValueError):
    """记录不满足契约。抛错而不是打日志 —— 脏数据进库比缺数据更贵。"""


# ── Event ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Event:
    """CTE 收集到的一个真实事件。Phase 1 的产物。

    ``system_version`` 是关键字段：CTE-1 的验收案例（征信漏拒）在当前 HEAD 上
    **已经修好了**，所以「复现旧行为」必须显式声明测的是哪个版本。
    没有这个字段，retro 里写「复现了漏拒」就成了无法证伪的断言。
    """

    event_id: str
    source: str
    surface: str
    title: str
    observed_at: str
    system_version: str
    input_case: str
    current_result: str
    expected_result: str
    notes: str = ""

    def __post_init__(self) -> None:
        if self.source not in EVENT_SOURCES:
            raise SchemaError(f"未知事件来源 {self.source!r}，合法取值：{list(EVENT_SOURCES)}")
        if self.surface not in EVENT_SURFACES:
            raise SchemaError(f"未知 surface {self.surface!r}，合法取值：{list(EVENT_SURFACES)}")
        if not self.event_id:
            raise SchemaError("event_id 不能为空")

    @property
    def learning_blocked(self) -> bool:
        """该事件所在面是否被数据缺口卡住（只能记录，不能产出学习结论）。

        判据委托给 :mod:`test_evolution.readiness` —— 就绪度只有一个事实来源，
        免得这里的 ``BLOCKED_SURFACES`` 与那张表各说各话。
        """
        from test_evolution.readiness import learning_allowed

        return not learning_allowed(self.surface)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["learning_blocked"] = self.learning_blocked
        return payload


# ── Prediction ────────────────────────────────────────────────────────────────

@dataclass
class Prediction:
    """盲预测。Phase 2 的产物，**写入后不可覆盖**。

    为什么要盲预测：CTE 的核心风险是 Agent 在看到答案之后声称
    「我一开始就知道」。先落盘预测、再执行、最后追加实际结果，
    让「预测准不准」成为一个**可被统计的历史事实**，而不是一段自述。

    ``actual_result`` / ``comparison`` 只能通过 :meth:`record_outcome` 追加，
    目的正是让「预测 → 结果」的时间顺序留在文件里。
    """

    prediction_id: str
    event_id: str
    predicted_result: str
    likely_failure: Tuple[str, ...]
    risk_area: Tuple[str, ...]
    confidence: float
    recorded_at: str
    actual_result: Optional[str] = None
    comparison: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise SchemaError(f"confidence 必须落在 [0, 1]，收到 {self.confidence!r}")
        if self.comparison and self.actual_result is None:
            raise SchemaError("有 comparison 却没有 actual_result —— 比对必须先有结果")

    @property
    def is_resolved(self) -> bool:
        """是否已经追加过实际结果。未闭合的预测不该参与「预测准确率」。"""
        return self.actual_result is not None

    @property
    def hit(self) -> Optional[bool]:
        """预测是否命中。未闭合时返回 ``None``（不是 ``False``）。"""
        if not self.is_resolved:
            return None
        return self.actual_result == self.predicted_result

    def record_outcome(
        self,
        actual_result: str,
        *,
        comparison: str = "",
        at: Optional[str] = None,
    ) -> None:
        """追加实际结果。**已闭合的预测不允许再改** —— 那是篡改记录。"""
        if self.is_resolved:
            raise SchemaError(
                f"预测 {self.prediction_id} 已经闭合，不允许覆盖实际结果"
            )
        self.actual_result = actual_result
        self.comparison = comparison

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["likely_failure"] = list(self.likely_failure)
        payload["risk_area"] = list(self.risk_area)
        payload["is_resolved"] = self.is_resolved
        payload["hit"] = self.hit
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Prediction":
        return cls(
            prediction_id=str(payload["prediction_id"]),
            event_id=str(payload["event_id"]),
            predicted_result=str(payload.get("predicted_result") or ""),
            likely_failure=tuple(payload.get("likely_failure") or ()),
            risk_area=tuple(payload.get("risk_area") or ()),
            confidence=float(payload.get("confidence") or 0.0),
            recorded_at=str(payload.get("recorded_at") or ""),
            actual_result=payload.get("actual_result"),
            comparison=str(payload.get("comparison") or ""),
        )


# ── Candidate ─────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """一个待验证的测试改进提议。Phase 6 的产物。

    ``evidence`` **必须非空** —— 方案第 24 节 Risk 4「没有证据支持的
    Reflection 不得生成 Candidate」。这条在这里是硬校验，不是建议：
    Candidate 数量爆炸的解药就是拒绝没有证据的提议。
    """

    candidate_id: str
    type: str
    title: str
    evidence: Tuple[str, ...]
    surface: str
    proposed_change: str
    created_at: str
    status: str = "candidate"
    checks: Dict[str, str] = field(default_factory=dict)
    validated_at: str = ""
    rejected_reason: str = ""
    approver: str = ""
    produced_asset: str = ""

    def __post_init__(self) -> None:
        if self.type not in CANDIDATE_TYPES:
            raise SchemaError(f"未知 Candidate 类型 {self.type!r}，合法取值：{list(CANDIDATE_TYPES)}")
        if self.status not in CANDIDATE_STATUSES:
            raise SchemaError(f"未知状态 {self.status!r}，合法取值：{list(CANDIDATE_STATUSES)}")
        if not self.evidence:
            raise SchemaError(
                f"Candidate {self.candidate_id} 没有任何 evidence —— "
                "没有证据支持的提议不得进入候选池（方案 Risk 4）"
            )
        if self.surface not in EVENT_SURFACES:
            raise SchemaError(f"未知 surface {self.surface!r}")

    @property
    def required_checks(self) -> Tuple[str, ...]:
        """本类型必须通过的检查项。来自 :data:`VALIDATION_MATRIX`。"""
        return VALIDATION_MATRIX[self.type]

    @property
    def is_machine_validated(self) -> bool:
        """机器验证是否全部通过。

        ``proposal_only`` **不算机器检查** —— 它不是一个「跑一遍就知道过没过」
        的检查，而是「这类改动根本不允许自动晋级」的标记。把它排除在本属性之外，
        是为了让 ``is_machine_validated`` 真的只回答「机器这一关过了没有」。
        """
        for check in self.required_checks:
            if check == "proposal_only":
                continue
            if self.checks.get(check) != "pass":
                return False
        return True

    @property
    def needs_human_review(self) -> bool:
        """是否需要人工批准。**当前每一类都需要**，没有例外（见常量注释）。"""
        return self.type in TYPES_REQUIRING_HUMAN_APPROVAL

    @property
    def is_proposal_only(self) -> bool:
        """只能形成提案、不允许晋级成 Validated Knowledge 的类型。

        方案第十二节：``RISK_RULE`` **禁止自动 Promotion 到 production**。
        """
        return "proposal_only" in self.required_checks

    @property
    def is_elevated(self) -> bool:
        """**晋级条件**：机器验证全过 + 有署名 + 不属于只能提案的类型。

        与 :attr:`can_promote` 的区别是它**不看当前状态** —— 所以
        ``write_validated`` 在 ``promote()`` 之后仍能用它来复检，
        而不会因为状态已经变成 ``validated`` 就自我否定。
        """
        if self.is_proposal_only:
            return False
        if not self.is_machine_validated:
            return False
        return bool(self.approver)

    @property
    def can_promote(self) -> bool:
        """**此刻**能否执行晋级动作（= 满足条件且还没被处理过）。

        **机器验证通过本身永远不足以晋级。** 方案第十一节条件 6 要求人工批准，
        且不存在 ``machine_validated → production`` 的直达路径 —— 这条没有例外，
        ``needs_human_review`` 对所有类型都为真。
        """
        if self.status in {"validated", "rejected", "deprecated"}:
            return False
        return self.is_elevated

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = list(self.evidence)
        payload["required_checks"] = list(self.required_checks)
        payload["is_machine_validated"] = self.is_machine_validated
        payload["needs_human_review"] = self.needs_human_review
        payload["is_proposal_only"] = self.is_proposal_only
        payload["is_elevated"] = self.is_elevated
        payload["can_promote"] = self.can_promote
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Candidate":
        return cls(
            candidate_id=str(payload["candidate_id"]),
            type=str(payload["type"]),
            title=str(payload.get("title") or ""),
            evidence=tuple(payload.get("evidence") or ()),
            surface=str(payload.get("surface") or ""),
            proposed_change=str(payload.get("proposed_change") or ""),
            created_at=str(payload.get("created_at") or ""),
            status=str(payload.get("status") or "candidate"),
            checks=dict(payload.get("checks") or {}),
            validated_at=str(payload.get("validated_at") or ""),
            rejected_reason=str(payload.get("rejected_reason") or ""),
            approver=str(payload.get("approver") or ""),
            produced_asset=str(payload.get("produced_asset") or ""),
        )


# ── 读写 ──────────────────────────────────────────────────────────────────────

def _write_json(path: Path, payload: Mapping[str, Any], *, overwrite: bool) -> Path:
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"{path} 已存在。CTE 的记录只允许追加，不允许覆盖 —— "
            "如果要更新状态，请用对应的状态迁移接口。"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_event(event: Event, *, root: Path = DEFAULT_EVOLUTION_DIR) -> Path:
    return _write_json(root / "events" / f"{event.event_id}.json", event.to_dict(), overwrite=False)


def write_prediction(
    prediction: Prediction, *, root: Path = DEFAULT_EVOLUTION_DIR
) -> Path:
    """落盘盲预测。**已存在即报错** —— 这是「预测不可覆盖」的落点。"""
    return _write_json(
        root / "predictions" / f"{prediction.prediction_id}.json",
        prediction.to_dict(),
        overwrite=False,
    )


def update_prediction(
    prediction: Prediction, *, root: Path = DEFAULT_EVOLUTION_DIR
) -> Path:
    """闭合预测（追加 actual_result / comparison）。

    与 :func:`write_prediction` 的区别是这里允许覆盖**已有文件** ——
    但 :meth:`Prediction.record_outcome` 只允许在未闭合时调用，
    所以「预测内容本身」依然改不了，能改的只是执行结果那一段。
    """
    if not prediction.is_resolved:
        raise SchemaError(
            f"预测 {prediction.prediction_id} 还没记录实际结果，"
            "不要用 update_prediction 覆盖盲预测"
        )
    return _write_json(
        root / "predictions" / f"{prediction.prediction_id}.json",
        prediction.to_dict(),
        overwrite=True,
    )


def write_candidate(candidate: Candidate, *, root: Path = DEFAULT_EVOLUTION_DIR) -> Path:
    return _write_json(
        root / "candidates" / f"{candidate.candidate_id}.json",
        candidate.to_dict(),
        overwrite=True,
    )


def write_retro(
    event_id: str, body: str, *, root: Path = DEFAULT_EVOLUTION_DIR
) -> Path:
    path = root / "retros" / f"{event_id}.md"
    if path.exists():
        raise FileExistsError(f"{path} 已存在 —— 复盘按事件唯一，不要覆盖历史结论")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def write_validated(
    candidate: Candidate, body: str, *, root: Path = DEFAULT_EVOLUTION_DIR
) -> Path:
    """把 Candidate 晋级成 Validated Knowledge。

    **只有 :attr:`Candidate.can_promote` 为真才允许写入。** 这是方案
    第十一节的 Promotion Gate 在代码里的落点：机器验证 + 人工批准，
    两条都满足才配进 ``validated/``（未来 RAG 的唯一索引源）。
    """
    if not candidate.is_elevated:
        raise SchemaError(
            f"Candidate {candidate.candidate_id} 不满足晋级条件："
            f"机器验证={candidate.is_machine_validated}，"
            f"需要人工批准={candidate.needs_human_review}，"
            f"批准人={candidate.approver or '（空）'}"
        )
    path = root / "validated" / f"{candidate.candidate_id}.md"
    if path.exists():
        raise FileExistsError(f"{path} 已存在")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def write_rejected(
    candidate: Candidate, *, reason: str, root: Path = DEFAULT_EVOLUTION_DIR
) -> Path:
    """记录被拒的 Candidate **连同原因**。

    方案第 27 节要求「rejected Candidate 保留原因」—— 被拒的提议同样是资产：
    它记录了「这条路试过、不行、为什么」，防止三个月后有人重走一遍。
    """
    if not reason:
        raise SchemaError(f"拒绝 {candidate.candidate_id} 必须写明 reason")
    candidate.status = "rejected"
    candidate.rejected_reason = reason
    payload = candidate.to_dict()
    payload["rejection_reason"] = reason
    return _write_json(
        root / "rejected" / f"{candidate.candidate_id}.json",
        payload,
        overwrite=True,
    )


def load_events(*, root: Path = DEFAULT_EVOLUTION_DIR) -> List[Event]:
    directory = Path(root) / "events"
    if not directory.is_dir():
        return []
    events: List[Event] = []
    for path in sorted(directory.glob("*.json")):
        payload = _read_json(path)
        events.append(
            Event(
                event_id=str(payload["event_id"]),
                source=str(payload["source"]),
                surface=str(payload["surface"]),
                title=str(payload.get("title") or ""),
                observed_at=str(payload.get("observed_at") or ""),
                system_version=str(payload.get("system_version") or ""),
                input_case=str(payload.get("input_case") or ""),
                current_result=str(payload.get("current_result") or ""),
                expected_result=str(payload.get("expected_result") or ""),
                notes=str(payload.get("notes") or ""),
            )
        )
    return events


def load_candidates(*, root: Path = DEFAULT_EVOLUTION_DIR) -> List[Candidate]:
    directory = Path(root) / "candidates"
    if not directory.is_dir():
        return []
    return [
        Candidate.from_dict(_read_json(path))
        for path in sorted(directory.glob("*.json"))
    ]


def load_predictions(*, root: Path = DEFAULT_EVOLUTION_DIR) -> List[Prediction]:
    directory = Path(root) / "predictions"
    if not directory.is_dir():
        return []
    return [
        Prediction.from_dict(_read_json(path))
        for path in sorted(directory.glob("*.json"))
    ]


# ── 状态迁移 ──────────────────────────────────────────────────────────────────

def apply_checks(
    candidate: Candidate, results: Mapping[str, str]
) -> Candidate:
    """写入检查结果。只接受 ``pass`` / ``fail`` / ``skipped``。

    ``skipped`` 与 ``fail`` 都不算通过 —— 允许 ``skipped`` 的存在是为了让
    「这项没测」和「这项测了没过」在记录上可区分，但两者都挡住晋级。
    """
    for name, outcome in results.items():
        if outcome not in {"pass", "fail", "skipped"}:
            raise SchemaError(f"检查 {name!r} 的结果 {outcome!r} 非法，只接受 pass/fail/skipped")
        if name not in candidate.required_checks:
            raise SchemaError(
                f"检查 {name!r} 不在 {candidate.type} 的验证矩阵里"
                f"（要求：{list(candidate.required_checks)}）"
            )
        candidate.checks[name] = outcome

    if candidate.is_machine_validated and candidate.status == "candidate":
        candidate.status = "machine_validated"
    return candidate


def promote(
    candidate: Candidate, *, approver: str, root: Path = DEFAULT_EVOLUTION_DIR
) -> Path:
    """人工批准并晋级。

    ``approver`` 非空是硬要求 —— 方案第十一节条件 6：不允许
    ``machine_validated → production`` 这条直达路径存在。
    """
    if not approver:
        raise SchemaError("晋级必须署名 —— 人工批准是 Promotion Gate 的一部分，不接受匿名")
    if candidate.is_proposal_only:
        raise SchemaError(
            f"Candidate {candidate.candidate_id} 属于「只能提案」类型"
            f"（{candidate.type}），不得晋级为 Validated Knowledge"
        )
    if not candidate.is_machine_validated:
        raise SchemaError(
            f"Candidate {candidate.candidate_id} 机器验证未全部通过，不得晋级："
            f"{validation_report(candidate)['checks']}"
        )
    candidate.approver = approver
    candidate.status = "validated"
    candidate.validated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return write_candidate(candidate, root=root)


def validation_report(candidate: Candidate) -> Dict[str, Any]:
    """给 candidate 出一份人可读的验证状态（含还没跑的检查项）。"""
    return {
        "candidate_id": candidate.candidate_id,
        "type": candidate.type,
        "status": candidate.status,
        "required_checks": list(candidate.required_checks),
        "checks": {name: candidate.checks.get(name, "pending") for name in candidate.required_checks},
        "is_machine_validated": candidate.is_machine_validated,
        "needs_human_review": candidate.needs_human_review,
        "is_proposal_only": candidate.is_proposal_only,
        "approver": candidate.approver,
        "can_promote": candidate.can_promote,
    }


__all__ = (
    "BLOCKED_SURFACES",
    "ROOT_DIR",
    "CANDIDATE_STATUSES",
    "CANDIDATE_TYPES",
    "DEFAULT_EVOLUTION_DIR",
    "EVENT_SOURCES",
    "EVENT_SURFACES",
    "VALIDATION_MATRIX",
    "Candidate",
    "Event",
    "Prediction",
    "SchemaError",
    "apply_checks",
    "load_candidates",
    "load_events",
    "load_predictions",
    "promote",
    "validation_report",
    "write_candidate",
    "write_event",
    "write_prediction",
    "write_rejected",
    "write_retro",
    "write_validated",
)
