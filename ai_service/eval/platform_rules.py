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


def compute_quality(image_path: str, *, snapshot: Any = None) -> Dict[str, Any]:
    """取这条样本的图像质量结果。

    ``snapshot`` 给出时**优先走快照**（``test_evolution.ocr_snapshot``）：
    这样 CI 完全不必读图，也就不再依赖「图片恰好还在检出里」。
    这不是可有可无的优化 —— ``.github/workflows/tests.yml`` 会删掉每类
    第 4 张及之后的图，而 golden 集的 ``*-03`` 五条样本恰好落在
    ``bank_card_0004.png`` 上：没有快照时，那五条在 CI 里的质量检测
    必然失败（``path.is_file()`` 不过），返回 ``quality_check_unavailable``。

    没有快照时回退到现读图片，保持本地开发的便利。
    图片缺失或不可读时返回空 dict —— 评测应当降级而不是崩掉。
    """
    if snapshot is not None:
        observed = snapshot.quality_for(image_path)
        if observed:
            return observed

    from app.quality_check import check_image_quality

    path = Path(image_path)
    if not path.is_file():
        return {}
    try:
        return check_image_quality(str(path))
    except (ValueError, OSError):
        return {}


def normalize_fields(doc_type: str, fields: Mapping[str, Any]) -> Dict[str, Any]:
    """把字段转成规则引擎能吃的形状。

    ``labels.json`` 的卡号是 ``"5415 9827 3869 1093"`` 带空格，
    而 ``app.rule_check.is_valid_card_number`` 要求纯数字 —— 不归一的话
    每张银行卡都会被误判成 ``invalid_card_number``。

    OCR 快照里的 ``parsed_fields`` 已经过 ``app.field_parser``，
    卡号本来就是纯数字，走这里是无害的幂等操作。
    """
    normalized = dict(fields)
    if doc_type == "bank_card":
        from app.field_parser import normalize_card_number

        card_number = normalized.get("card_number")
        if isinstance(card_number, str):
            normalized["card_number"] = normalize_card_number(card_number)
    return normalized


def fields_for(
    sample: GoldenSample, *, snapshot: Any = None
) -> Tuple[Dict[str, Any], str]:
    """取这条样本的字段输入，返回 ``(字段, 来源)``。

    来源是 ``"ocr_snapshot"`` 或 ``"labels"`` —— 这个标记要进报告：
    「结论正确率 0.775」在两种输入下的含义完全不同，不写明来源的数字
    会被后来者当成同一件事比较。

    **快照未命中时不在本函数里抛错。** 快照的 strict 策略由
    ``SnapshotReplay`` 决定（它在未命中时已经抛了），这里再抛一次
    只会让错误信息重复。拿不到就用标注真值，并在返回值里如实标出来。
    """
    if snapshot is not None:
        observed = snapshot.fields_for(sample.image_path)
        if observed:
            return observed, "ocr_snapshot"
    return dict(sample.fields), "labels"


def platform_verdict(
    sample: GoldenSample,
    fields: Optional[Mapping[str, Any]] = None,
    *,
    snapshot: Any = None,
) -> Tuple[str, List[str], Dict[str, Any]]:
    """算出平台对这条样本会给出的结论。

    返回 ``(review_result, review_reasons, quality)``。质量检测结果一并带回，
    供调用方填进 AI 上下文（带上原始指标即可让 AI 服务从 ``flags`` 模式切到
    ``metrics`` 模式做真重算）。

    ``fields`` 显式给出时优先使用（评测编排层可能已经从快照取过）；
    否则按 ``snapshot`` 与否决定从快照还是标注取。
    """
    from app.rule_check import review_bank_card_with_reasons

    if fields is None:
        resolved_fields, _source = fields_for(sample, snapshot=snapshot)
    else:
        resolved_fields = fields

    resolved = normalize_fields(sample.doc_type, resolved_fields)
    quality = compute_quality(sample.image_path, snapshot=snapshot)
    if not quality:
        return "review", ["quality_check_unavailable"], quality

    # 快照给了原始文本行时一并传下去 —— 规则层据此区分
    # 「OCR 没认出来」与「解析器没取到」（见 app/ocr_evidence.py）。
    # 没有快照（本地无图路径）时传空串，规则层行为与以前一致。
    ocr_text = _ocr_text_for(sample, snapshot=snapshot)

    if sample.doc_type == "bank_card":
        verdict, reasons = review_bank_card_with_reasons(
            resolved, quality, ocr_text=ocr_text
        )
        return verdict, reasons, quality

    # id_card 的规则在 app.main 里，按面别要求不同字段
    from app.main import review_id_card_with_reasons

    side = sample.id_card_side or resolved.get("_side") or _side_from_fields(resolved)
    verdict, reasons = review_id_card_with_reasons(
        side, resolved, quality, ocr_text=ocr_text
    )
    return verdict, reasons, quality


def _ocr_text_for(sample: GoldenSample, *, snapshot: Any = None) -> str:
    """取这条样本的原始 OCR 文本，供规则层做归因。取不到返回空串。"""
    if snapshot is None:
        return ""
    observed = snapshot.snapshot.get(sample.image_path)
    if observed is None:
        return ""
    return "\n".join(observed.ocr_texts)


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


def findings_summary(sample: GoldenSample, verdict: str, reasons: Sequence[str]) -> str:
    """一句话说明这条样本「平台看到了什么 vs 标注说是什么」。

    用于 OCR 快照的 diff 报告 —— 快照的真实价值不是「OCR 准确率」，
    而是「哪几条样本的**结论**因为输入真实化而变了」。
    """
    return (
        f"{sample.sample_id}: 标注 {','.join(sample.expected_reason_codes) or '正常'}"
        f" / 平台实测 {','.join(reasons) or '正常'} => {verdict}"
    )
