"""在评测里调用**平台真实规则引擎**算出审核结论。

为什么需要这个模块
------------------
``GoldenSample.to_review_context()`` 原本把结论推定成
``"pass" if normal else "review"`` —— 那是个占位符，产不出 ``reject``。
``verdict_accuracy``（决策层）比较的是人工标注与这个占位符，
所以它测的是「标注与占位符的一致率」，不是「标注与平台的一致率」。
人工标注 26/40 是 ``reject``，指标因此被结构性锁死。

本模块把真实结论接上：跑平台的质量检测拿到原始指标，再把字段真值交给
平台的规则引擎，得到平台**实际会给出**的结论。

架构约束
--------
``ai_service`` 其余模块刻意**不 import ``app``**（见 ``thresholds.py`` 的
模块 docstring：AI 服务要保持轻依赖、可独立部署，而 ``app`` 拖着 FastAPI +
OpenCV）。本模块是唯一的例外，所以它被单独隔离出来，且只由评测 CLI 惰性
import —— 纯离线的 CI 路径不受影响。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ai_service.eval.golden import GoldenSample


def compute_quality(image_path: str) -> Dict[str, Any]:
    """跑平台的质量检测。

    图片缺失或不可读时返回空 dict —— 评测应当降级而不是崩掉。
    """
    from app.quality_check import check_image_quality

    path = Path(image_path)
    if not path.is_file():
        return {}
    try:
        return check_image_quality(str(path))
    except (ValueError, OSError):
        return {}


def normalize_fields(doc_type: str, fields: Mapping[str, Any]) -> Dict[str, Any]:
    """把 labels.json 的字段真值转成规则引擎能吃的形状。

    ``labels.json`` 的卡号是 ``"5415 9827 3869 1093"`` 带空格，
    而 ``app.rule_check.is_valid_card_number`` 要求纯数字 —— 不归一的话
    每张银行卡都会被误判成 ``invalid_card_number``。
    """
    normalized = dict(fields)
    if doc_type == "bank_card":
        from app.field_parser import normalize_card_number

        card_number = normalized.get("card_number")
        if isinstance(card_number, str):
            normalized["card_number"] = normalize_card_number(card_number)
    return normalized


def platform_verdict(
    sample: GoldenSample,
    fields: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, List[str], Dict[str, Any]]:
    """算出平台对这条样本会给出的结论。

    返回 ``(review_result, review_reasons, quality)``。质量检测结果一并带回，
    供调用方填进 AI 上下文（带上原始指标即可让 AI 服务从 ``flags`` 模式切到
    ``metrics`` 模式做真重算）。
    """
    from app.rule_check import review_bank_card_with_reasons

    resolved = normalize_fields(
        sample.doc_type,
        sample.fields if fields is None else fields,
    )
    quality = compute_quality(sample.image_path)
    if not quality:
        return "review", ["quality_check_unavailable"], quality

    if sample.doc_type == "bank_card":
        verdict, reasons = review_bank_card_with_reasons(resolved, quality)
        return verdict, reasons, quality

    # id_card 的规则在 app.main 里，按面别要求不同字段
    from app.main import review_id_card_with_reasons

    side = sample.id_card_side or _side_from_fields(resolved)
    verdict, reasons = review_id_card_with_reasons(side, resolved, quality)
    return verdict, reasons, quality


def platform_dual_judge(
    sample: GoldenSample,
    review_result: str,
    review_reasons: Sequence[str],
    quality: Mapping[str, Any],
    *,
    client: Any = None,
) -> Tuple[str, Dict[str, Any]]:
    """跑平台的双判编排，返回 ``(最终结论, 双判字段)``。

    评测要测的**不是**「LLM 说了什么」，而是「双判这个机制在真实规则结论上
    行为是否正确」—— 边界判据有没有挑对样本、失败有没有回落、改判有没有落库。
    所以这里直接复用 ``app.adjudication.maybe_adjudicate``，不另写一套。

    ``client`` 默认在离线环境下是 AIDisabled 的客户端（返回降级），
    于是 ``llm_failure_rate`` 会如实报出「复核没跑成」——
    这正是评测应当暴露的事实，而不是伪造一个漂亮的改判率。
    """
    from app.adjudication import maybe_adjudicate

    return maybe_adjudicate(
        client=client,
        request_id=sample.sample_id,
        doc_type=sample.doc_type,
        review_result=review_result,
        review_reasons=review_reasons,
        fields=sample.fields,
        quality=quality,
    )


def _side_from_fields(fields: Mapping[str, Any]) -> str:
    """字段真值里没有面别信息时的兜底推断。"""
    if "issue_authority" in fields or "valid_period" in fields:
        return "back"
    if "id_number" in fields or "address" in fields:
        return "front"
    return "unknown"
