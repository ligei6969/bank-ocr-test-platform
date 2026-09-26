"""双判编排：把边界判据、AI 复核与最终结论串起来。

职责
----
* 决定哪些 ``review`` 值得问 AI（委托 ``app.boundary``）；
* 问 AI 并拿到复核意见（委托 ``AIAssistClient``）；
* **用业务规则算出最终结论**（:func:`apply_adjudication`）。

最后一条是这里存在的理由。规则引擎必须保持纯确定性，所以不能在
``rule_check`` 里塞 AI 调用；而「最终结论怎么算」也不该散落在路由函数里，
否则 ``app/main.py`` 会慢慢变成谁都不想打开的文件。

安全不变式
----------
**规则拥有否决权，LLM 只拥有有限的放行建议权。**

* ``reject`` 永不被改（连复核都不会被触发）；
* ``pass`` 不调 AI；
* 只有 ``review`` 且命中边界判据时，才可能因 AI 意见降为 ``pass``；
* AI 的任何失败（不可用/超时/输出非法/异常）都回落规则原判。

``apply_adjudication`` **绝不**写 ``return ai_decision`` —— 即使 AI 返回了
意料之外的字符串，业务层也必须兜住。schema 是防线，不是边界的全部。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

from app.boundary import detect_boundary_case

logger = logging.getLogger(__name__)


def apply_adjudication(rule_result: str, ai_decision: Optional[str]) -> str:
    """由规则结论与 AI 意见算出最终结论。

    这是**唯一**允许把 ``review`` 降为 ``pass`` 的地方，且只在这两种情形下：
    ``rule_result == "review"`` 且 ``ai_decision == "pass"``。其余一切原样返回。
    """
    if rule_result != "review":
        return rule_result
    if ai_decision == "pass":
        return "pass"
    return rule_result


def maybe_adjudicate(
    *,
    client: Any = None,
    request_id: str,
    doc_type: str,
    review_result: str,
    review_reasons: Sequence[str],
    fields: Mapping[str, Any] | None,
    quality: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """对一条记录做双判，返回 ``(最终结论, 落库用的双判字段)``。

    **永不抛异常** —— 复核是增强，不能让审核链路因它出错。
    AI 关闭、超时、熔断、返回垃圾，结果都一样：维持规则原判。

    ``client`` 允许为 ``None``：只有在判定为边界样本后才去取客户端。
    这样非边界记录（绝大多数）连「构造客户端」这一步都省掉。
    """
    skipped = {
        "llm_invoked": False,
        "llm_override": False,
        "llm_decision": "",
        "llm_fallback_reason": "",
        "boundary_criteria": [],
        "llm_rationale": "",
    }

    criteria = detect_boundary_case(
        fields, quality, review_result, review_reasons, doc_type=doc_type
    )
    if not criteria:
        # 绝大多数记录走这里：非 review、或 review 但不属于边界样本。
        # 不调 AI 是成本控制，也是「reject 不该有被复核机会」的权限边界。
        return review_result, skipped

    if client is None:
        from app.ai_client import get_ai_client

        client = get_ai_client()

    payload = {
        "request_id": request_id,
        "doc_type": doc_type,
        "review_result": review_result,
        "review_reasons": list(review_reasons),
        "boundary_criteria": criteria,
        "quality_metrics": dict((quality or {}).get("quality_metrics") or {}),
        "field_findings": _field_findings(fields, review_reasons),
    }

    try:
        verdict = client.adjudicate(payload)
    except Exception as exc:  # noqa: BLE001 - 复核失败不该冒泡到审核链路
        logger.exception("双判调用异常 request_id=%s error=%s", request_id, exc)
        verdict = {
            "decision": review_result,
            "overrode": False,
            "degraded": True,
            "reason": "client_error",
            "rationale": "",
        }

    final = apply_adjudication(review_result, str(verdict.get("decision") or ""))
    record = {
        "llm_invoked": True,
        "llm_override": bool(final == "pass" and review_result == "review"),
        "llm_decision": str(verdict.get("decision") or ""),
        "llm_fallback_reason": str(verdict.get("reason") or ""),
        "boundary_criteria": list(criteria),
        "llm_rationale": str(verdict.get("rationale") or ""),
    }
    logger.info(
        "双判 request_id=%s rule=%s final=%s override=%s criteria=%s degraded=%s",
        request_id,
        review_result,
        final,
        record["llm_override"],
        criteria,
        bool(verdict.get("degraded")),
    )
    return final, record


def _field_findings(
    fields: Mapping[str, Any] | None,
    review_reasons: Sequence[str],
) -> list[str]:
    """把字段层的形态判定转成文字结论交给模型。

    **只传结论，不传原始值** —— 出站 payload 会被 ``_sanitize_payload`` 脱敏，
    卡号只留前 6 后 4 位，让模型看残缺的号码去判断「差几位」是没意义的。
    所以形态判断在 :mod:`app.boundary` 做完，这里只做转述。
    """
    codes = {str(code) for code in (review_reasons or [])}
    findings: list[str] = []
    resolved = fields or {}

    if "invalid_card_number" in codes:
        raw = resolved.get("card_number")
        digits = "".join(ch for ch in str(raw) if ch.isdigit()) if raw is not None else ""
        findings.append(f"卡号校验未通过，长度为 {len(digits)} 位（期望 16-19 位）")
    if "invalid_valid_date" in codes:
        findings.append("有效期校验未通过（格式形如 MM/YY，月份须在 01-12）")
    for code in sorted(codes):
        if code.startswith("missing_"):
            findings.append(f"字段未解析出来：{code[len('missing_'):]}")
    return findings
