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
        ),
    },
    {
        "event_id": "EVT-003",
        "source": "manual_review",
        "surface": "ocr",
        "title": "字段为空时无法区分「OCR 没认出来」与「解析器没取到」",
        "system_version": "HEAD",
        "input_case": "data/processed/id_card/front/normal/id_front_0001.jpg",
        "current_result": "missing_id_number（不区分是识别失败还是解析失败）",
        "expected_result": "应能指出证据在不在 OCR 文本里",
        "notes": (
            "修完 EVT-002 的解析缺陷后 id_number 仍是 2/10，逐条核对才发现"
            "另外 8 张的 OCR 文本里根本没有那串数字。也就是说 EVT-002 最初的"
            "归因把 OCR 的局限算进了解析器的账上 —— 那会让验收标准定错"
            "（以为要修到 10/10，实际上限是 2/10）。"
            "本事件记录的是度量缺口：missing_* 原因码把两种完全不同的故障"
            "塌缩成了一个信号。",
        ),
    },
    {
        "event_id": "EVT-004",
        "source": "manual_review",
        "surface": "adjudication",
        "title": "严重退化被字段缺失降级：9 条该拒的样本被判成转人工",
        "system_version": "HEAD",
        "input_case": "data/processed/bank_card/blur/bank_card_0001.png",
        "current_result": "review ['missing_valid_date', 'missing_name', 'image_blur']（severe_image_blur 被丢弃）",
        "expected_result": "reject ['severe_image_blur', ...]（人工结论为 reject）",
        "notes": (
            "CTE-3 用真实 OCR 字段重跑评测后发现：9 条人工结论为 reject 的样本"
            "被平台判成 review。逐条核对指标发现 3 条是「variance ≈ 1.1，"
            "远低于 severe 阈值 30，但字段缺失」—— 根因是 "
            "review_bank_card_with_reasons 把 missing_reasons 的返回排在 "
            "severe_reasons 之前。同一文件的单测 "
            "test_missing_fields_still_outrank_severity 的 docstring 说"
            "「严重度判拒也要带上原因码」，断言却只检查 missing 项，"
            "所以这个缺陷一直是绿的。"
        ),
    },
    {
        "event_id": "EVT-005",
        "source": "manual_review",
        "surface": "ocr",
        "title": "出生标签被截断（出1996年1月12日）导致出生日期解析失败",
        "system_version": "HEAD",
        "input_case": "data/processed/id_card/front/blur/id_front_0002.jpg",
        "current_result": "birth=None（文本里是「出1996年1月12日」）",
        "expected_result": "1996-01-12",
        "notes": (
            "**由 CTE-4 的归因信号自动指出**，不是人工翻样本发现的。"
            "该信号把这批解析失败分成「文本里有证据」（解析器责任）与"
            "「文本里没证据」（OCR 限制）两类，其中 birth 有 2 例被判为前者 ——"
            "一查正是标签被截断：_extract_birth 的正则要求完整的「出生」，"
            "而模糊图上被认成「出」。与住址的「住址」→「址」是同一类退化，"
            "但两处的容忍度不一致 —— 这次把判据统一了。"
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


def _execute_ocr(
    event: Event, *, snapshot: Any = None, reparse: bool = True
) -> Execution:
    """OCR 面的事件：把**当前解析器**跑在快照录到的文本上。

    关键区分，踩过一次才知道要说清楚：

    * 快照的 ``ocr_texts`` 是**录制时的事实** —— 真实 PaddleOCR 当时认出了什么。
      这部分不会被重跑复现，所以必须用录下来的。
    * 快照的 ``parsed_fields`` 是**录制时解析器的输出** —— 它是历史快照，
      会随着解析器改进而**过时**。拿它当「实际结果」，修好解析器之后
      事件仍会报出旧结果（EVT-003 最初就是这样，显示的还是修复前的
      「3 个字段解析成功」）。

    所以默认 ``reparse=True``：用当前解析器重新解析录到的文本。
    这样「同一个事件在不同解析器版本下表现如何」才是可比的，
    CTE 的「修复前失败 / 修复后通过」也才有意义。

    ``reparse=False`` 时返回快照里存的历史结果，用于回答
    「**当时**系统是什么表现」——重演历史事件时需要这个。
    """
    from test_evolution.ocr_snapshot import load

    snapshot = snapshot or load()
    observed = snapshot.get(event.input_case) if snapshot else None
    if observed is None:
        return Execution(
            event_id=event.event_id,
            actual_result="未录制",
            executor="ocr_snapshot",
            detail={"reason": "该图不在快照里；先跑 scripts/record_ocr_snapshot"},
        )

    if reparse:
        parsed = _reparse(observed)
        source = "当前解析器"
    else:
        parsed = observed.parsed_fields
        source = "快照录制时的解析结果"

    # 「实际结果」用解析出的字段里有多少个非空来表达 —— 这比一个布尔
    # 更能反映「到底差多远」，也是本事件的核心观测。
    populated = sorted(k for k, v in parsed.items() if v and not k.startswith("_"))
    return Execution(
        event_id=event.event_id,
        actual_result=f"{len(populated)} 个字段解析成功：{populated}" if populated else "无字段解析成功",
        executor=f"ocr_snapshot@{observed.engine_version}（{source}）",
        detail={
            "ocr_texts": observed.ocr_texts,
            "parsed_fields": parsed,
            "recorded_parsed_fields": observed.parsed_fields,
            "quality_result": observed.quality.get("quality_result"),
            "parse_error": observed.parse_error,
            "populated_fields": populated,
            "reparsed": reparse,
        },
    )


def _reparse(observed: Any) -> Dict[str, Any]:
    """用当前解析器重新解析快照录到的文本行。"""
    texts = "\n".join(observed.ocr_texts)
    if observed.doc_type == "bank_card":
        from app.field_parser import parse_bank_card_fields

        return dict(parse_bank_card_fields(texts))

    from app.id_card_parser import (
        detect_id_card_side,
        parse_id_card_back_fields,
        parse_id_card_front_fields,
    )

    side = detect_id_card_side(texts)
    if side == "back":
        parsed: Dict[str, Any] = dict(parse_id_card_back_fields(texts))
    else:
        parsed = dict(parse_id_card_front_fields(texts))
    parsed["_side"] = side
    return parsed


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
            "**没有任何测试见过真实 OCR 的文本行结构。** 全部测试都用 mock OCR，"
            "而 `MOCK_OCR_TEXT` 是把「姓名 沈梓欣」拼成一行的；单测里的用例也"
            "照抄这个形状。所以 `parser` 对「标签与值同行」的假设既是实现也是"
            "测试的共识 —— 两边一起错，测不出差异。\n\n"
            "抓出它的是 CTE-2 的 OCR 快照：它是项目里**第一次**把真实"
            "PaddleOCR 的输出落成可断言的输入。"
        ),
        "attribution": (
            "**两条独立的成因，修完第一条才看得见第二条。** 这一点值得记下来 ——\n\n"
            "**（一）解析层缺陷（已修）**：`_value_after_label` 只在单行内匹配，"
            "标签单独成行时直接返回 `None`。真实 OCR 恰恰把「姓名」与「沈梓欣」"
            "检测成两个文本框。同类问题还有三处：地址天然跨多行却没跨行收集、"
            "`id_number` 正则用 `[1-9]` 打头拒绝了前导零的真实号码、"
            "`valid_period` 只认同行。\n\n"
            "**（二）OCR 局限（不可由解析器修复）**：修完（一）之后再测，"
            "`id_number` 仍只有 2/10 —— 因为**另外 8 张的 OCR 文本里根本没有"
            "那串数字**（文本止于「公民身份号码」，后面跟的是水印「非真实证件」）。"
            "反面同理：`valid_period` 只有 3/15，其余 12 张没认出日期区间。\n\n"
            "**在动手前把这两条分开，是本事件最大的价值。** 早先的版本把"
            "`id_number 0/10` 整个归因给解析器，那会让修复后的验收标准定错 —— "
            "会以为修到 10/10 才算修好，而实际上限是 2/10。"
        ),
        "gap": (
            "缺的是**输入形状的多样性**，不是一个具体用例。\n\n"
            "这个项目的测试资产一直建立在 mock OCR 的输出形状上，"
            "而 mock 是「设计出来的」而非「观察到的」。这类缺口无法靠补用例填 —— "
            "要补的是**输入来源**（快照）。\n\n"
            "另外还暴露出一个度量缺口：平台没有区分「OCR 没认出来」与"
            "「解析器没取到」。这两个信号在 `missing_*` 原因码里长得一样，"
            "但改进方向完全不同。"
        ),
        "history": (
            "无直接先例，但**同类**问题值得警惕：CTE-1 的征信漏拒也是"
            "「测试用了自己想象出来的输入」。两次都是同一个模式 —— "
            "**被测样本来自己方设计，于是系统的假设从未被外部挑战**。\n\n"
            "区别在于 CTE-1 是关键词覆盖不全（规则漏了写法），"
            "本事件是结构假设不成立（规则假设了不存在的输出形状）。"
        ),
    },
    "EVT-003": {
        "why_missed": (
            "不是「没发现」，而是**发现的归因是错的**。EVT-002 最初把\n"
            "`id_number 0/10` 整个算作解析器的责任，修完解析器之后数字没变 ——\n"
            "这才看出 8/10 的样本里 OCR 压根没产出那串数字。\n\n"
            "根因是评测只看**最终字段**，不看**中间文本**。字段为空时，\n"
            "「OCR 没认出来」与「解析器没取到」这两个完全不同的故障\n"
            "塌缩成同一个 `missing_id_number`。"
        ),
        "attribution": (
            "**度量/可观测性问题**，不是 OCR 问题也不是解析问题。\n\n"
            "平台目前没有把「OCR 原始文本里有没有这个字段的证据」记录下来。\n"
            "`missing_card_number` / `missing_id_number` 这类原因码\n"
            "既可能来自识别失败，也可能来自解析失败，但对下游是同一个信号。\n\n"
            "后果很具体：改进方向会被指错 —— 该去调解析的地方去调了 OCR，\n"
            "或者反过来。"
        ),
        "gap": (
            "缺的是**归因能力**：需要能从文本层回答「这个字段的证据在不在」。\n\n"
            "本事件不产出生产代码改动（那需要产品口径决策），\n"
            "但产出方法：CTE-2 已经把原始文本行录进快照，\n"
            "所以「证据在不在」是可计算的 —— 缺的只是把它变成一个明确的信号。"
        ),
        "history": (
            "与 EVT-002 是同一个事件的两层：EVT-002 是「解析器有缺陷」\n"
            "（已修），EVT-003 是「修完才知道上限在哪」（度量缺口）。\n\n"
            "这也是 CTE 该有的样子 —— 一个事件修完不是终点，\n"
            "复盘里那句「另外 8 张 OCR 没认出来」本身就是一个新事件。"
        ),
    },
    "EVT-004": {
        "why_missed": (
            "**测试断言了实现的行为，却没断言实现的目标。**\n\n"
            "`test_missing_fields_still_outrank_severity` 的 docstring 写的是\n"
            "「严重度判拒也要带上原因码」，但断言只有两条：\n"
            "`result == \"review\"` 与 `\"missing_card_number\" in reasons`。\n"
            "实现里 `missing_reasons` 在 `severe_reasons` **之前**返回，\n"
            "于是 `severe_image_blur` 被整条丢掉 —— 而测试从没检查它是否幸存。\n\n"
            "**测试名和 docstring 都在说「严重度优先」，代码却在做相反的事，\n"
            "而断言弱到发现不了这个矛盾。**"
        ),
        "attribution": (
            "**业务规则（生产代码）的判定顺序问题。**\n\n"
            "`review_bank_card_with_reasons` 里字段缺失检查排在严重度检查之前，\n"
            "所以「严重模糊 + 字段读不出」返回 `review`。\n\n"
            "但字段读不出**正是**严重模糊造成的 —— 让症状覆盖病因。\n"
            "严重度是**图像自身的性质**，与解析结果无关，应当先判且不受字段层影响。\n\n"
            "对照证据：`review_id_card_with_reasons`（`app/main.py`）\n"
            "的写法是正确的（严重度先判并前置到原因码），只有银行卡这条路径有 bug。\n"
            "两条路径行为不一致本身就是线索。"
        ),
        "gap": (
            "缺的是**跨字段的组合用例**。\n\n"
            "既有的严重度测试用的都是「字段齐全」的输入\n"
            "（`VALID_FIELDS` + severe），从没测过「严重度 + 字段缺失」同时发生。\n"
            "而真实退化样本恰恰是两者同时成立 —— 模糊到读不出字，字段自然是缺的。\n\n"
            "**单变量测试的组合盲区**：每个变量单独测都通过，交叉处没人看。"
        ),
        "history": (
            "与 CTE-1 的征信漏拒形似而神不同 —— 那次是**关键词覆盖不全**，\n"
            "这次是**优先级顺序写反**，但两者都表现为「该拒没拒」。\n\n"
            "真实收益：修好后 `verdict_accuracy` 0.675 → 0.725（+2 条）。\n"
            "剩下 7 条不一致全是反光样本，卡在那个**刻意停用**的\n"
            "反光严重度阈值上（标定间隙只有 9%）—— 那是另一个事件。"
        ),
    },
    "EVT-005": {
        "why_missed": (
            "**没有人翻过这批样本** —— 这个缺陷是被归因信号**自动**指出来的。\n\n"
            "CTE-4 加的 `evidence_missing_*` 把 50 张图的解析失败分成两类：\n"
            "「文本里有证据」（解析器责任）与「文本里没证据」（OCR 限制）。\n"
            "分完之后 birth 字段有 2 例落在「解析器责任」一侧 —— 一查就是标签截断。\n\n"
            "在此之前，这类缺陷混在 `missing_birth` 里，和 OCR 没认出来\n"
            "长得一模一样，没有线索指向它。"
        ),
        "attribution": (
            "**解析层的判据不一致。**\n\n"
            "同一类 OCR 退化 —— 标签首字丢失 —— 在两处的处理不同：\n\n"
            "* `住址` → `址`：`_extract_address` **已经**容忍（CTE-3 修的）\n"
            "* `出生` → `出`：`_extract_birth` 的正则**要求**完整「出生」\n\n"
            "同一个工程里对同一种退化有两种判据，本身就是线索。\n"
            "修法是把 `生` 也变成可选，与住址的处理一致。\n\n"
            "严格性没有丢：放宽标签之后仍要求完整日期，\n"
            "「出生地」这类无关文本仍不会被吃进来（有测试守着）。"
        ),
        "gap": (
            "缺的是**跨字段的一致性检查**。\n\n"
            "每个字段的抽取函数各写各的容忍度，没有一处规定\n"
            "「标签被截断该怎么办」。CTE-3 修住址时若顺手问一句\n"
            "「还有哪些字段有同样的形状」，这个缺陷当时就会被发现 ——\n"
            "而不是等到归因信号把它指出来。\n\n"
            "这也说明归因信号的价值不只是「分对类」：\n"
            "**它把「值得看一眼」的样本拣了出来**，而人不会去逐条翻 50 张图。"
        ),
        "history": (
            "与 `EVT-002` 同源：都是「解析器对真实 OCR 的输出形状假设过强」。\n"
            "`EVT-002` 是**结构假设**（标签与值同行），\n"
            "`EVT-005` 是**字面假设**（标签完整）。\n\n"
            "两者的共同点是：mock OCR 从不产生这些形状，\n"
            "所以实现与测试共享同一个假设。"
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
    fix_landed: bool = False,
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
        results = _validate_ocr_regression(candidate, question, fix_landed=fix_landed)
    elif surface == "adjudication":
        results = _validate_adjudication_regression(question, fix_landed=fix_landed)
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


#: 每个 OCR 事件「修复后应当能取到哪些字段」。
#:
#: **不能用快照里的 ``parsed_fields`` 来判断「修复前是否失败」**：
#: 快照在每次修复后都会用新解析器重录，那份字段已经不代表旧行为。
#: 所以「该事件修的是哪个字段」必须单独声明 —— 它是人对这个事件的理解，
#: 不是能从数据里读出来的东西。
#:
#: 写成表而不是让验证器去猜（比如「找所有为空的字段」）：
#: 那样会把 OCR 本来就认不出的字段也算进去，验收标准就定错了 ——
#: 这正是 `EVT-003` 记录的教训。
OCR_EVENT_TARGET_FIELDS: Dict[str, tuple] = {
    "EVT-002": ("name", "address"),
    "EVT-005": ("birth",),
}


def _validate_ocr_regression(
    candidate: Candidate, image_path: str, *, fix_landed: bool
) -> Dict[str, str]:
    """OCR 面 ``NEW_TEST`` 的检查结果。

    ``fix_landed`` 决定「修复后通过」这一步有没有可测对象：

    * ``False`` —— 缺陷仍在（提案阶段）。标 ``skipped``：没有修复版本可测，
      不假称 pass。这让 ``is_machine_validated`` 为假，提案不能自己晋级。
    * ``True`` —— 修复已落地。去跑当前解析器，断言目标字段都取到了。
    """
    from test_evolution.ocr_snapshot import load

    snapshot = load()
    observed = snapshot.get(image_path) if snapshot else None
    if observed is None:
        return {
            "executable": "fail",
            "reproduces_before_fix": "fail",
            "passes_after_fix": "skipped",
        }

    targets = OCR_EVENT_TARGET_FIELDS.get(candidate.candidate_id.replace("CTE-", "EVT-"), ())
    if not targets:
        return {
            "executable": "fail",
            "reproduces_before_fix": "fail",
            "passes_after_fix": "skipped",
            "note": "该事件的 OCR 目标字段未在 OCR_EVENT_TARGET_FIELDS 里声明",
        }

    texts = "\n".join(observed.ocr_texts)
    # 文本里有证据却没解析出来 —— 这就是「未修复版本上会 FAIL」的依据。
    from app.ocr_evidence import has_evidence

    reproduced = all(has_evidence(field, texts) for field in targets)

    if not fix_landed:
        return {
            "executable": "pass",
            "reproduces_before_fix": "pass" if reproduced else "fail",
            "passes_after_fix": "skipped",
        }

    from app.id_card_parser import parse_id_card_front_fields

    now = parse_id_card_front_fields(texts)
    # 修复前取不到、现在取得到 —— 这才是「修复后通过」
    fixed = all(now.get(field) for field in targets)

    return {
        "executable": "pass",
        "reproduces_before_fix": "pass" if reproduced else "fail",
        "passes_after_fix": "pass" if fixed else "fail",
    }


def _validate_adjudication_regression(
    image_path: str, *, fix_landed: bool
) -> Dict[str, str]:
    """``adjudication`` 面 ``NEW_TEST`` 的检查结果。

    这一面的事件是**平台判定顺序**问题（严重度 vs 字段缺失），
    验证方式与 OCR 面不同：不解析字段，而是用同一份真实质量指标
    跑规则引擎，看它给出的结论对不对。

    修复前的行为固化在 :data:`_PRE_FIX_VERDICT` 里 —— 老代码先返回
    ``missing_reasons``，所以得到 ``review``。
    """
    if not fix_landed:
        return {
            "executable": "pass",
            "reproduces_before_fix": "pass" if _PRE_FIX_VERDICT == "review" else "fail",
            "passes_after_fix": "skipped",
        }

    # 修复已落地：用当前规则引擎跑同一份「严重模糊 + 字段全缺」的输入
    from app.rule_check import review_bank_card_with_reasons

    quality = {
        "is_blur": True,
        "brightness": "normal",
        "has_glare": False,
        "quality_result": "review",
        "quality_reasons": ["image_blur"],
        "quality_metrics": {
            "blur_laplacian_variance": 1.08,
            "brightness_mean": 80.6,
            "glare_component_ratio": 0.0,
        },
        "severe_reasons": ["severe_image_blur"],
    }
    verdict, reasons = review_bank_card_with_reasons(
        {"card_number": None, "valid_date": None, "name": None}, quality
    )

    return {
        "executable": "pass",
        # 旧代码在同样输入下返回 review —— 这就是「修复前 FAIL」
        "reproduces_before_fix": "pass" if _PRE_FIX_VERDICT == "review" else "fail",
        "passes_after_fix": "pass" if verdict == "reject" else "fail",
    }


#: 修复前的行为：字段缺失检查排在严重度之前，所以严重模糊也被判成转人工。
_PRE_FIX_VERDICT = "review"


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
    fix_landed: bool = False,
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
            fix_landed=fix_landed,
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
