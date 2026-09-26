"""双判复核：对规则判定的边界样本做一次受限的 LLM 复核。

职责边界（很重要）
------------------
本模块**只回答一个问题**：规则判的 ``review``，是否属于误报、可否放行。

它**不能**：
* 加重结论（输出模型限死为 ``review`` / ``pass``，结构上产不出 ``reject``）；
* 改变非边界样本（那是调用方的职责，见 ``app/adjudication.py`` 的业务层兜底）；
* 在失败时给出任何结论（一律回落到规则原判）。

为什么输出模型用 ``Literal`` 而不是靠 prompt 劝阻
------------------------------------------------
prompt 是「请求模型别这么做」，schema 是「模型做不到这么做」。
安全属性应当由结构保证，而不是由措辞保证 —— 后者在对抗输入下没有约束力。
所以 ``AdjudicationVerdict.decision`` 是 ``Literal["review", "pass"]``。

即便如此，**调用方仍须再兜一层**：schema 可能被改、mock 可能写错、
测试可能构造异常值。防线不嫌多，但每一层都要知道自己防的是什么。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

from ai_service.llm import LLMClient, LLMUnavailableError
from ai_service.prompts import PromptTemplate, register
from ai_service.structured import StructuredOutputError, complete_structured

logger = logging.getLogger(__name__)


ADJUDICATE_PROMPT = register(
    PromptTemplate(
        id="adjudicate_boundary",
        version="v1",
        purpose="对规则判定的边界样本做受限复核：只能维持 review 或降为 pass",
        system=(
            "你是银行影像审核的复核员。规则引擎已把这条记录判为「待人工复核」，"
            "你的职责是判断它是否属于规则的误报、可否直接放行。"
            "你**只能**给出 review 或 pass，**无权加重结论** —— 拒绝与否则由上游规则决定。"
            "若认为确实需要人工复核，回 review；只有确信是规则误报时才回 pass。"
            "所有阈值判断一律以给定的原始指标为准，不得编造数字。"
            "拿不准时选择 review —— 多一次人工复核的成本，远低于错误放行。"
        ),
        body=(
            "【文档类型】\n{doc_type}\n\n"
            "【规则结论】\n{review_result}\n\n"
            "【审核原因码】\n{review_reasons}\n\n"
            "【命中的边界判据】\n{boundary_criteria}\n"
            "（这些说明为什么这条记录被挑出来复核，不代表规则一定错了）\n\n"
            "【原始影像指标】\n{quality_metrics}\n\n"
            "【字段形态诊断】\n{field_findings}\n\n"
            "【相关知识】\n{knowledge}\n\n"
            "只输出一个 JSON 对象，不要任何解释性文字，例如：\n"
            '{{"decision":"pass","confidence":0.7,'
            '"rationale":"反光位于卡片镭射区，关键字段已完整解析",'
            '"risk_notes":["建议抽样复查"]}}'
        ),
    )
)


class AdjudicationVerdict(BaseModel):
    """模型的复核结论。``decision`` 限死两个值 —— 结构上无法加重结论。"""

    decision: Literal["review", "pass"] = Field(
        ..., description="复核意见：维持人工复核，或认为可放行"
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(default="", description="一句话理由")
    risk_notes: List[str] = Field(default_factory=list)


#: 复核失败的原因码。与平台侧 ``llm_fallback_reason`` 列一一对应。
REASON_NO_MODEL = "llm_unavailable"
REASON_UNPARSEABLE = "unparseable_output"


def _as_mapping(hit: Any) -> Mapping[str, Any]:
    """把检索结果统一成 mapping。

    ``retriever.search()`` 返回的是 ``RetrievalHit`` 数据类，而测试里常用
    dict 造假数据 —— 两种都要能处理。只认 dict 会让真实链路直接炸，
    而假数据恰好掩盖了这一点（这个 bug 就是 --live 时才暴露的）。
    """
    if isinstance(hit, Mapping):
        return hit
    to_dict = getattr(hit, "to_dict", None)
    if callable(to_dict):
        mapped = to_dict()
        if isinstance(mapped, Mapping):
            return mapped
    return {
        "title": str(getattr(hit, "title", "") or ""),
        "doc_id": str(getattr(hit, "doc_id", "") or ""),
        "content": str(getattr(hit, "content", "") or ""),
    }


def _format_knowledge(hits: Sequence[Any]) -> str:
    if not hits:
        return "（无）"
    lines: List[str] = []
    for raw in hits[:3]:
        hit = _as_mapping(raw)
        title = str(hit.get("title") or hit.get("doc_id") or "")
        content = str(hit.get("content") or "").strip()
        if content:
            lines.append(f"- {title}：{content[:400]}")
    return "\n".join(lines) if lines else "（无）"


def _degraded(
    reason: str,
    *,
    request_id: str,
    doc_type: str,
    rule_decision: str,
    started: float,
    llm_name: str,
    llm_available: bool,
) -> Dict[str, Any]:
    """失败时的统一形状：**永远回落到规则原判**，绝不给出自己的结论。"""
    return {
        "request_id": request_id,
        "doc_type": doc_type,
        "decision": rule_decision,
        "rule_decision": rule_decision,
        "overrode": False,
        "available": False,
        "degraded": True,
        "reason": reason,
        "confidence": 0.0,
        "rationale": "",
        "risk_notes": [],
        "engine": {
            "llm": llm_name,
            "llm_available": llm_available,
            "generation": "fallback",
        },
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
    }


async def adjudicate(
    *,
    llm: LLMClient,
    request_id: str,
    doc_type: str,
    review_result: str,
    review_reasons: Sequence[str],
    boundary_criteria: Sequence[str],
    quality_metrics: Optional[Mapping[str, Any]] = None,
    field_findings: Optional[Sequence[str]] = None,
    knowledge_hits: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """对边界样本做一次复核。**任何失败都回落到规则原判，不抛异常。**

    返回形状与平台侧 ``review_records`` 的新列直接对应：
    ``decision`` / ``overrode`` / ``reason`` / ``rationale``。
    """
    started = time.monotonic()
    degraded_args = dict(
        request_id=request_id,
        doc_type=doc_type,
        rule_decision=review_result,
        started=started,
        llm_name=getattr(llm, "name", "none"),
        llm_available=bool(getattr(llm, "available", False)),
    )

    # 非边界样本不该走到这里；真走到了也不能改结论
    if review_result != "review":
        return _degraded("not_applicable", **degraded_args)

    if not getattr(llm, "available", False):
        # NullLLMClient 的 complete 会抛异常，必须先判 available
        return _degraded(REASON_NO_MODEL, **degraded_args)

    prompt = ADJUDICATE_PROMPT.render(
        doc_type=doc_type,
        review_result=review_result,
        review_reasons=", ".join(str(c) for c in review_reasons) or "（无）",
        boundary_criteria=", ".join(str(c) for c in boundary_criteria) or "（无）",
        quality_metrics=dict(quality_metrics or {}) or "（无）",
        field_findings=", ".join(str(f) for f in (field_findings or [])) or "（无）",
        knowledge=_format_knowledge(knowledge_hits or []),
    )

    try:
        verdict = await complete_structured(
            llm,
            prompt,
            AdjudicationVerdict,
            system=ADJUDICATE_PROMPT.system,
            max_tokens=400,
        )
    except StructuredOutputError as exc:
        logger.warning("复核输出无法解析 request_id=%s error=%s", request_id, exc)
        return _degraded(REASON_UNPARSEABLE, **degraded_args)
    except LLMUnavailableError as exc:
        logger.warning("复核模型不可用 request_id=%s error=%s", request_id, exc)
        return _degraded(REASON_NO_MODEL, **degraded_args)
    except Exception as exc:  # noqa: BLE001 - 模型侧异常一律降级，不让它冒泡
        logger.exception("复核调用异常 request_id=%s error=%s", request_id, exc)
        return _degraded("llm_error", **degraded_args)

    return {
        "request_id": request_id,
        "doc_type": doc_type,
        "decision": verdict.decision,
        "rule_decision": review_result,
        "overrode": verdict.decision == "pass",
        "available": True,
        "degraded": False,
        "reason": "",
        "confidence": verdict.confidence,
        "rationale": verdict.rationale,
        "risk_notes": list(verdict.risk_notes),
        "engine": {
            "llm": getattr(llm, "name", "none"),
            "llm_available": True,
            "generation": "llm",
        },
        "prompt": ADJUDICATE_PROMPT.label,
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
    }
