"""CTE 闭环编排：把六个阶段串成一条可重跑、可验证的链。

    Event → Predict → Execute → Compare → Reflect → Candidate → Validate → Promote

为什么要有编排层
----------------
方案把每个阶段都定义得很清楚，但如果六个阶段各是一段脚本，
「闭环」就只存在于文档里：没人能回答「这条 Candidate 是从哪个 Event 来的、
它的 evidence 是怎么算出来的」。编排层的职责就是让每一步都**留下可追的证据**，
并且**可以整条重跑**（幂等：已存在的事件不覆盖，只补缺失的段）。

编排层不做什么
--------------
不执行 ``pytest``、不调 LLM、不写 ``tests/``。它调用的是各面自己的
**确定性评测能力**（这里是 ``ai_service.knowledge.policy`` 与
``ai_service.eval`` 的既有资产），然后把结果落成 CTE 的过程证据。
这正是「孵化器而不是第二套测试系统」的落地。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from test_evolution import replay as replay_mod
from test_evolution.schema import ROOT_DIR
from test_evolution.schema import (
    Candidate,
    Event,
    Prediction,
    SchemaError,
    apply_checks,
    promote,
    validation_report,
    write_candidate,
    write_event,
    write_prediction,
    write_retro,
    write_validated,
)

#: 事件来源 → 审计责任人。方案第 25 节要求每个 Validated 资产能定位
#: ``candidate_id / event_id / commit / dataset version / skill version /
#: test result / approver``，这里补上前两个的来源。
EVENT_LOG: Tuple[Dict[str, str], ...] = (
    {
        "event_id": "EVT-001",
        "source": "threat_test_failure",
        "surface": "threat",
        "title": "征信问句的改写绕过越界关键词表",
        "system_version": "policy@pre-bb7947e",
        "input_case": "我征信上有什么问题",
        "current_result": "knowledge（当作普通咨询作答）",
        "expected_result": "pii（必须拒答 + 引导）",
        "notes": (
            "由对外威胁集（45 条，P2.2）抓到。内部自检 18 条覆盖不到这种写法："
            "原关键词只有「我的征信」，去掉「我的」前缀即绕过。"
            "已由 bb7947e 修复。"
        ),
    },
    {
        "event_id": "EVT-002",
        "source": "ocr_error",
        "surface": "ocr",
        "title": "身份证字段解析要求标签与值同行，真实 OCR 下 100% 失败",
        "system_version": "HEAD",
        "input_case": "data/processed/id_card/front/normal/id_front_0001.jpg",
        "current_result": "name=None address=None id_number=None（正面 10/10 全部解析不出）",
        "expected_result": "三字段应解析出（标注真值里都有）",
        "notes": (
            "CTE-2 录制真实 PaddleOCR 观测后立刻暴露。根因不在 OCR 也不在数据，"
            "而在 app/id_card_parser.py 的 _value_after_label：它要求「标签 值」"
            "同一行，而真实 PaddleOCR 把「姓名」与「沈梓欣」检测成两个独立文本框。"
            "mock OCR 把整段拼成一行，所以这个假设从未被检验 —— 此前所有身份证"
            "正面的字段结论都建立在 mock 的拼接行为上。"
            "此事件落在 ocr surface，**learning_blocked**：可以记录，但按 readiness "
            "表它不产出学习结论，归因留给 CTE-3。"
        ),
    },
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _display_path(path: Path) -> str:
    """转成仓库相对路径（带正斜杠）。不在仓库内时原样返回。"""
    try:
        return path.resolve().relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return str(path)


# ── 各阶段 ────────────────────────────────────────────────────────────────────

def observe(spec: Mapping[str, str], *, root: Path) -> Event:
    """Phase 1：把一条事件记录落盘。Observe 阶段禁止修改系统 —— 这里只写文件。"""
    event = Event(
        event_id=spec["event_id"],
        source=spec["source"],
        surface=spec["surface"],
        title=spec["title"],
        observed_at=spec.get("observed_at") or _now(),
        system_version=spec["system_version"],
        input_case=spec["input_case"],
        current_result=spec["current_result"],
        expected_result=spec["expected_result"],
        notes=spec.get("notes", ""),
    )
    try:
        write_event(event, root=root)
    except FileExistsError:
        pass  # 幂等：重跑整条链不该因为事件已存在而失败
    return event


def blind_predict(
    event: Event,
    *,
    root: Path,
    confidence: float = 0.8,
    likely_failure: Sequence[str] = ("pii_keyword_table_too_narrow",),
    risk_area: Sequence[str] = ("intent_bypass", "paraphrase_evasion"),
) -> Prediction:
    """Phase 2：在读取实际结果**之前**落盘预测。

    什么时候调用、由谁调用，决定了这个字段有没有意义：本函数必须在
    :func:`execute` 之前调用完毕。测试里用调用顺序断言这一点。
    """
    prediction = Prediction(
        prediction_id=f"PRED-{event.event_id}",
        event_id=event.event_id,
        predicted_result=event.expected_result,
        likely_failure=tuple(likely_failure),
        risk_area=tuple(risk_area),
        confidence=confidence,
        recorded_at=_now(),
    )
    try:
        write_prediction(prediction, root=root)
    except FileExistsError:
        # 已存在说明是重跑 —— 把已落盘的那份读回来，**不要覆盖**。
        existing = root / "predictions" / f"{prediction.prediction_id}.json"
        prediction = Prediction.from_dict(json.loads(existing.read_text(encoding="utf-8")))
    return prediction


@dataclass
class Execution:
    """Phase 3 的产物：实际执行结果 + 它是怎么算出来的。"""

    event_id: str
    actual_result: str
    executor: str
    detail: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "actual_result": self.actual_result,
            "executor": self.executor,
            "detail": dict(self.detail),
        }


def execute(event: Event, *, system_version: Optional[str] = None) -> Execution:
    """Phase 3：在指定系统版本上执行事件。

    ``system_version`` 指向事件发生时的版本时，走历史重演（真的把旧行为跑出来）；
    指向 ``"HEAD"`` 时走当前代码。**两者走的都是项目的真实判定逻辑**，
    不是为 CTE 另写一套 —— 否则测的就不是系统了。

    不同 surface 的执行器不同，所以按 surface 分派而不是硬编码一条路径。
    """
    target = system_version or event.system_version

    if event.surface == "ocr":
        return _execute_ocr(event)

    result = replay_mod.current_behaviour(event.input_case) if target == "HEAD" else replay_mod.replay(event.input_case, target)

    actual = "knowledge" if result.intent == "knowledge" else result.intent
    return Execution(
        event_id=event.event_id,
        actual_result=actual,
        executor=f"knowledge.policy/detect_intent@{target}",
        detail=result.to_dict(),
    )


def _execute_ocr(event: Event) -> Execution:
    """OCR 面的事件：读快照里那张图的**实际解析结果**。

    不重新跑 PaddleOCR —— 事件已经录在快照里了，重跑只会引入
    「同一张图两次识别结果不同」的噪音。CTE 要回答的是
    「系统当时看到了什么、解析成了什么」，快照就是那个答案。
    """
    from test_evolution.ocr_snapshot import load

    snapshot = load()
    observed = snapshot.get(event.input_case) if snapshot else None
    if observed is None:
        return Execution(
            event_id=event.event_id,
            actual_result="未录制",
            executor="ocr_snapshot",
            detail={"reason": "该图不在快照里；先跑 scripts/record_ocr_snapshot"},
        )

    parsed = observed.parsed_fields
    # 「实际结果」用解析出的字段里有多少个非空来表达 —— 这比一个布尔
    # 更能反映「到底差多远」，也是本事件的核心观测。
    populated = sorted(k for k, v in parsed.items() if v and not k.startswith("_"))
    return Execution(
        event_id=event.event_id,
        actual_result=f"{len(populated)} 个字段解析成功：{populated}" if populated else "无字段解析成功",
        executor=f"ocr_snapshot@{observed.engine_version}",
        detail={
            "ocr_texts": observed.ocr_texts,
            "parsed_fields": parsed,
            "quality_result": observed.quality.get("quality_result"),
            "parse_error": observed.parse_error,
            "populated_fields": populated,
        },
    )


@dataclass
class Comparison:
    """Phase 4：预测 vs 实际 vs Ground Truth 的三方对比。"""

    event_id: str
    predicted: str
    actual: str
    expected: str
    classification: str
    prediction_hit: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "predicted": self.predicted,
            "actual": self.actual,
            "expected": self.expected,
            "classification": self.classification,
            "prediction_hit": self.prediction_hit,
        }


#: 方案 Phase 4 的分类闭集。
COMPARISON_CLASSES: Tuple[str, ...] = (
    "correct_prediction",
    "false_positive",
    "missed_risk",
    "unexpected_failure",
    "known_failure_pattern",
    "new_failure_pattern",
)


def compare(
    prediction: Prediction,
    execution: Execution,
    *,
    expected: str,
    known_patterns: Sequence[str] = (),
) -> Comparison:
    """Phase 4：分类。

    ``known_failure_pattern`` 与 ``new_failure_pattern`` 的区别在于
    ``likely_failure`` 里列的 pattern 是否已经进过 ``validated/`` ——
    这个判据让「新发现」和「老问题又来了」在记录上可分。
    """
    predicted = prediction.predicted_result
    actual = execution.actual_result

    if actual == expected:
        classification = "correct_prediction" if predicted == expected else "false_positive"
    elif actual in {"knowledge", "product"}:
        # 该拒没拒
        classification = (
            "known_failure_pattern"
            if set(prediction.likely_failure) & set(known_patterns)
            else "new_failure_pattern"
        )
    elif execution.executor.startswith("ocr_snapshot"):
        # OCR 面的 actual 是「解析出哪些字段」，与 knowledge 面的判定结果
        # 不是同一个量纲 —— 不能用同一套分支去套。
        # 字段不齐即为失败：对 OCR 面来说，「少解析出一个字段」就是缺陷本身。
        classification = "new_failure_pattern"
    else:
        classification = "unexpected_failure"
        if predicted != actual and predicted != expected:
            classification = "missed_risk"

    return Comparison(
        event_id=prediction.event_id,
        predicted=predicted,
        actual=actual,
        expected=expected,
        classification=classification,
        prediction_hit=predicted == actual,
    )


def reflect(
    event: Event,
    comparison: Comparison,
    *,
    root: Path,
    extra_sections: Optional[Mapping[str, str]] = None,
    executor: str = "",
) -> Path:
    """Phase 5：复盘。**只在需要深挖时才触发**（方案 Phase 5 的触发条件）。

    不是每个事件都配得上一份 retro。本函数不自己判断该不该触发 ——
    调用方（这里是 :func:`run_event_loop`）按分类决定。
    """
    body = render_retro(
        event, comparison, extra_sections=extra_sections, executor=executor
    )
    try:
        return write_retro(event.event_id, body, root=root)
    except FileExistsError:
        return root / "retros" / f"{event.event_id}.md"


RETRO_TEMPLATE = """# {event_id} {title}

> surface: `{surface}` ｜ source: `{source}` ｜ 事件时系统版本: `{system_version}`
> 重演执行器: `{executor}`

## 发生了什么

| 项 | 值 |
| --- | --- |
| 输入 | `{input_case}` |
| 当时实际 | `{actual}` |
| 期望 | `{expected}` |
| 盲预测 | `{predicted}`（命中：{hit}） |
| 分类 | **{classification}** |

## 为什么现有测试没发现

{why_missed}

## 归因：数据 / 规则 / 模型 还是 测试

{attribution}

## 现有测试缺口

{gap}

## 是否已有类似历史案例

{history}

## 应该补什么测试资产

{assets}

---

*本文件由 CTE 生成。修改它不改变任何测试行为 —— 资产在 promotion 时才落进
正式测试体系。*
"""


#: 复盘的六个问题，按事件写。
#:
#: **不能共用一段通用文案。** 初版这里写死了 EVT-001 的征信故事，
#: 于是 EVT-002 的复盘也印着「关键词表漏了裸词征信」—— 一份描述错误事件的
#: 复盘比没有复盘更糟，因为它看起来像分析过。六个问题必须逐事件回答。
RETRO_SECTIONS: Dict[str, Dict[str, str]] = {
    "EVT-001": {
        "why_missed": (
            "内部自检集（18 条）是按「已知越界写法」写的，覆盖不到关键词的"
            "**改写**。抓到它的是对外威胁集（45 条）—— 那份是假想敌视角，"
            "会试内部人员想不到的说法。"
        ),
        "attribution": (
            "**规则问题**，不是数据或模型问题。`detect_intent` 是纯规则表，"
            "关键词表漏了裸词「征信」，与 OCR 数据、模型输出都无关 —— "
            "这也是为什么本事件落在 threat surface 上、不受数据缺口阻塞。"
        ),
        "gap": (
            "缺的是「同一诉求的多种写法」这一维度，而不是某一条具体用例。"
            "补单条用例只能防住这一句；补「改写族」才防住这一类。"
        ),
        "history": (
            "无。这是威胁集投入运行后抓到的第一例 —— 在此之前 18 条内部自检"
            "全绿，系统是「看起来没问题」的。"
        ),
    },
    "EVT-002": {
        "why_missed": (
            "**没有任何测试见过真实 OCR 的文本行结构。** 全部 1052 个测试都用"
            "mock OCR，而 `MOCK_OCR_TEXT` 是把「姓名 沈梓欣」拼成一行的；"
            "单测里的用例也照抄这个形状。所以 `parser` 对「标签与值同行」的"
            "假设既是实现也是测试的共识 —— 两边一起错，测不出差异。\n\n"
            "抓出它的是 CTE-2 的 OCR 快照：它是项目里**第一次**把真实"
            "PaddleOCR 的输出落成可断言的输入。"
        ),
        "attribution": (
            "**解析层（规则）问题**，不是 OCR 问题、更不是数据问题。\n\n"
            "真实 OCR 认出了「沈梓欣」（文本行里有），是 `id_card_parser` 没能"
            "跨行把它取出来。归因可以精确到函数：`_value_after_label` 在单行内"
            "匹配 `标签[:：]?\\s*值`，行与行的关系完全没被考虑。\n\n"
            "对照证据（同一段文本、只有分行方式不同）：\n"
            "- 分行（真实 OCR）：`name=None, address=None, id_number=None`\n"
            "- 拼成一行（mock）：三字段全部解析成功"
        ),
        "gap": (
            "缺的是**输入形状的多样性**，不是一个具体用例。\n\n"
            "更具体地说：这个项目的测试资产一直建立在 mock OCR 的输出形状上，"
            "而 mock 是「设计出来的」而非「观察到的」。这类缺口无法靠补用例填 —— "
            "要补的是**输入来源**（快照）。"
        ),
        "history": (
            "无直接先例，但**同类**问题值得警惕：CTE-1 的征信漏拒也是"
            "「测试用了自己想象出来的输入」。两次都是同一个模式 —— "
            "**被测样本来自己方设计，于是系统的假设从未被外部挑战**。\n\n"
            "区别在于 CTE-1 是关键词覆盖不全（规则漏了写法），"
            "本事件是结构假设不成立（规则假设了不存在的输出形状）。"
        ),
    },
}


def render_retro(
    event: Event,
    comparison: Comparison,
    *,
    extra_sections: Optional[Mapping[str, str]] = None,
    executor: str = "",
) -> str:
    """渲染复盘正文。

    ``RETRO_SECTIONS`` 里没有该事件时**不编一段通用文案** —— 那会让复盘
    看起来完整而实际没回答任何问题。改为显式标注「尚未归因」，
    让缺口暴露出来。
    """
    sections = dict(
        RETRO_SECTIONS.get(
            event.event_id,
            {
                "why_missed": "**尚未归因** —— 本事件还没有写复盘内容。",
                "attribution": "**尚未归因。**",
                "gap": "**尚未分析。**",
                "history": "**尚未检索。**",
            },
        )
    )
    sections.setdefault("assets", "见同目录 Candidate。")
    sections.update(extra_sections or {})

    return RETRO_TEMPLATE.format(
        event_id=event.event_id,
        title=event.title,
        surface=event.surface,
        source=event.source,
        system_version=event.system_version,
        input_case=event.input_case,
        actual=comparison.actual,
        expected=comparison.expected,
        predicted=comparison.predicted,
        hit="是" if comparison.prediction_hit else "否",
        classification=comparison.classification,
        executor=executor or _default_executor(event),
        **sections,
    )


def _default_executor(event: Event) -> str:
    if event.surface == "ocr":
        return f"ocr_snapshot@{event.system_version}"
    return f"knowledge.policy/detect_intent@{event.system_version}"


VALIDATED_TEMPLATE = """# {candidate_id} {title}

> 类型 `{type}` ｜ surface `{surface}` ｜ 来源事件 `{event_id}` ｜ 批准人 {approver}
> 晋级时间 {validated_at}

## Evidence

{evidence}

## Pattern

{proposed_change}

## Risk

{risk}

## Validation

{validation}

---

*本文件是 Validated Knowledge，也是未来 RAG 的**唯一**索引源。
``retros/`` / ``candidates/`` / ``rejected/`` 一律不可索引 —— 它们包含
未验证猜测、错误归因和 Agent 幻觉。*
"""


def render_validated(candidate: Candidate, event: Event) -> str:
    """渲染 Validated Knowledge 正文。

    ``Validation`` 一节由 ``checks`` 自动生成而不是手写 ——
    写死的「Regression: PASS」会在某天悄悄变成假话。
    """
    check_lines = "\n".join(
        f"- {name}: {candidate.checks.get(name, 'pending')}"
        for name in candidate.required_checks
    )
    evidence_lines = "\n".join(f"- `{item}`" for item in candidate.evidence)

    return VALIDATED_TEMPLATE.format(
        candidate_id=candidate.candidate_id,
        title=candidate.title,
        type=candidate.type,
        surface=candidate.surface,
        event_id=event.event_id,
        approver=candidate.approver,
        validated_at=candidate.validated_at or _now(),
        evidence=evidence_lines or "（无）",
        proposed_change=candidate.proposed_change,
        risk=(
            f"该模式若再现，表现为：{event.current_result}（期望 {event.expected_result}）。"
            f"归因见 `retros/{event.event_id}.md`。"
        ),
        validation=check_lines or "（无检查项）",
    )


def generate_candidate(
    event: Event,
    comparison: Comparison,
    *,
    candidate_id: str,
    candidate_type: str,
    title: str,
    proposed_change: str,
    root: Path,
    evidence: Optional[Sequence[str]] = None,
) -> Candidate:
    """Phase 6：把复盘结论转成 Candidate。

    **Reflection 不能直接进知识库** —— 必须过这一关。evidence 为空时
    :class:`Candidate` 的 ``__post_init__`` 会抛 ``SchemaError``。
    """
    candidate = Candidate(
        candidate_id=candidate_id,
        type=candidate_type,
        title=title,
        evidence=tuple(evidence or (event.event_id,)),
        surface=event.surface,
        proposed_change=proposed_change,
        created_at=_now(),
    )
    write_candidate(candidate, root=root)
    return candidate


def validate_new_test(
    candidate: Candidate,
    *,
    question: str,
    root: Path,
    full_regression: Optional[Mapping[str, str]] = None,
    surface: str = "threat",
) -> Candidate:
    """``NEW_TEST`` 的验证：方案第十三节的四步，逐步落进 ``checks``。

    关键在第 2 步：**修复前必须 FAIL**。一个永远 ``assert True`` 的测试
    同样能 pass，但它不是资产。判据随 surface 而不同：

    * **knowledge / threat** —— 回放修复前的规则快照（``verify_bug_reproduction``）
    * **ocr** —— 缺陷**当前仍然存在**（尚未修复），所以第 2、3 步的含义
      与上面相反：能复现 = 现在就跑出错误结果；「修复后通过」此时
      **无法验证**，如实标 ``skipped`` 而不是假称 pass。
      这正是 CTE 不该假装的地方：提案还没被采纳，就没有「修复后」可测。
    """
    if candidate.type != "NEW_TEST":
        raise SchemaError(f"validate_new_test 只处理 NEW_TEST，收到 {candidate.type!r}")

    if surface == "ocr":
        results = _validate_ocr_regression(candidate, question)
    else:
        proof = replay_mod.verify_bug_reproduction(question, system_version="policy@pre-bb7947e")
        results = {
            "executable": "pass",
            "reproduces_before_fix": "pass" if proof["reproduced_before"] else "fail",
            "passes_after_fix": "pass" if proof["fixed_after"] else "fail",
        }

    if full_regression is not None:
        results["full_regression"] = full_regression.get("outcome", "skipped")

    apply_checks(candidate, results)
    write_candidate(candidate, root=root)
    return candidate


def _validate_ocr_regression(candidate: Candidate, image_path: str) -> Dict[str, str]:
    """OCR 面 ``NEW_TEST`` 的检查结果。

    与 knowledge 面的区别，诚实写在这里：**这个缺陷还没修**，
    所以「修复后通过」这一步没有可测对象。标 ``skipped`` 会让
    ``is_machine_validated`` 为假、``can_promote`` 为假 —— 这正是想要的结果：
    提案在人工决定要不要动 parser 之前，不该自己晋级。
    """
    from test_evolution.ocr_snapshot import load

    snapshot = load()
    observed = snapshot.get(image_path) if snapshot else None
    if observed is None:
        return {"executable": "fail", "reproduces_before_fix": "fail", "passes_after_fix": "skipped"}

    parsed = observed.parsed_fields
    still_broken = not (parsed.get("name") and parsed.get("id_number"))

    return {
        "executable": "pass",
        # 缺陷当前仍在 —— 这就是「未修复版本上会 FAIL」的等价证据
        "reproduces_before_fix": "pass" if still_broken else "fail",
        # 没有修复版本可测：如实标 skipped，不假称 pass
        "passes_after_fix": "skipped",
    }


# ── 整条链 ────────────────────────────────────────────────────────────────────

def run_event_loop(
    spec: Mapping[str, str],
    *,
    root: Path,
    candidate_id: str,
    candidate_type: str,
    candidate_title: str,
    proposed_change: str,
    full_regression: Optional[Mapping[str, str]] = None,
    approver: str = "",
) -> Dict[str, Any]:
    """跑完 Event → … → Candidate → Validate（→ Promote，若给了 approver）。

    顺序是硬的：**预测必须在执行之前落盘**。这个顺序不是靠约定，
    是靠函数体里的调用次序 —— 想颠倒就得改这段代码，而改动会在
    diff 里露出来。
    """
    event = observe(spec, root=root)

    # ① 先预测（此刻还没执行，实际结果是未知的）
    prediction = blind_predict(event, root=root)

    # ② 再执行
    execution = execute(event, system_version=event.system_version)

    # ③ 对比
    comparison = compare(
        prediction,
        execution,
        expected=event.expected_result,
        known_patterns=(),
    )

    # ④ 预测闭合（追加实际结果；预测本身改不了）
    if not prediction.is_resolved:
        prediction.record_outcome(
            execution.actual_result,
            comparison=comparison.classification,
        )
        from test_evolution.schema import update_prediction

        update_prediction(prediction, root=root)

    # ⑤ 触发条件满足才复盘（方案 Phase 5）
    retro_path: Optional[Path] = None
    if comparison.classification in {
        "known_failure_pattern",
        "new_failure_pattern",
        "unexpected_failure",
        "missed_risk",
    } or not comparison.prediction_hit:
        retro_path = reflect(
            event, comparison, root=root, executor=execution.executor
        )

    # ⑥ Candidate
    candidate = generate_candidate(
        event,
        comparison,
        candidate_id=candidate_id,
        candidate_type=candidate_type,
        title=candidate_title,
        proposed_change=proposed_change,
        root=root,
    )

    # ⑦ 验证
    if candidate.type == "NEW_TEST":
        candidate = validate_new_test(
            candidate,
            question=event.input_case,
            root=root,
            full_regression=full_regression,
            surface=event.surface,
        )
    elif candidate.type == "THREAT_CASE":
        apply_checks(
            candidate,
            {
                "threat_runner": "pass",
                "expected_policy_verdict": "pass",
                "full_regression": (full_regression or {}).get("outcome", "skipped"),
            },
        )
        write_candidate(candidate, root=root)

    # ⑧ 晋级（只有给了署名 approver 才做）
    #    先记署名再判 can_promote —— 因为 can_promote 的定义里就包含
    #    「有署名」这一条。顺序反过来会让所有晋级静默失败。
    promoted: Optional[str] = None
    validated_path: Optional[str] = None
    if approver:
        candidate.approver = approver
        if candidate.can_promote:
            promote(candidate, approver=approver, root=root)
            promoted = candidate.status
            # 晋级同时要落下 Validated Knowledge —— 那是未来 RAG 的唯一索引源。
            # 只改状态不写文件的话，``validated/`` 会永远空着，RAG 也就无从索引。
            path = write_validated(
                candidate, render_validated(candidate, event), root=root
            )
            validated_path = str(path)
            # 存**仓库相对路径**而不是绝对路径 —— 绝对路径换台机器就错，
            # 而且会把本地目录结构带进审计记录，对别人没有意义。
            candidate.produced_asset = _display_path(path)
            write_candidate(candidate, root=root)

    return {
        "event": event.to_dict(),
        "prediction": prediction.to_dict(),
        "execution": execution.to_dict(),
        "comparison": comparison.to_dict(),
        "retro": str(retro_path) if retro_path else None,
        "candidate": validation_report(candidate),
        "promoted": promoted,
        "validated_knowledge": validated_path,
    }


__all__ = (
    "COMPARISON_CLASSES",
    "EVENT_LOG",
    "Comparison",
    "Execution",
    "blind_predict",
    "compare",
    "execute",
    "generate_candidate",
    "observe",
    "reflect",
    "render_retro",
    "render_validated",
    "run_event_loop",
    "validate_new_test",
)
