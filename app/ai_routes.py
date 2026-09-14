"""AI 复核助手的平台侧接口。

只有管理端能访问。用户端刻意不暴露 —— 与 ``user_home.html`` 里
「不展示 OCR 原文、完整证件号码或内部审核原因」的安全边界保持一致。

接口列表
--------
* ``GET  /ai/status``                 客户端状态（熔断状态、是否启用）
* ``GET  /ai/status?probe=true``      额外探测一次 AI 服务的 ``/health``
* ``POST /ai/explain/{request_id}``   对指定审核记录生成解释与处置建议

设计要点
--------
* **降级也是 200。** AI 服务不可用时返回 ``available=False`` 的正常响应，
  而不是 5xx —— 前端需要区分「记录不存在（404）」和「AI 没回答（200 + degraded）」。
* **不缓存 AI 结果到数据库。** P0 阶段解释是即时生成的，落库会带来
  「解释与记录版本不一致」的问题。落库留到 P2 与 ``llm_override`` 一起做。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.ai_client import AIAssistClient, get_ai_client
from app.auth_routes import require_admin_api_user
from app.csrf import validate_csrf_request
from app.review_records import get_review_record

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


def _client() -> AIAssistClient:
    return get_ai_client()


@router.get("/status")
def ai_status(
    request: Request,
    probe: bool = Query(default=False),
    _admin: dict = Depends(require_admin_api_user),
) -> dict:
    """返回客户端状态；``probe=true`` 时额外探测 AI 服务。"""
    client = _client()
    status = client.status()
    status["service"] = client.health() if probe else None
    return status


@router.post("/explain/{request_id}")
def explain_review_record(
    request: Request,
    request_id: str,
    _admin: dict = Depends(require_admin_api_user),
    _csrf_valid: None = Depends(validate_csrf_request),
) -> dict:
    """对一条审核记录生成 AI 解释。

    依赖顺序有意为之：鉴权排在 CSRF 之前，匿名请求得到 401 而不是 403，
    与 ``/bank-card/review`` 的现有约定保持一致。

    记录不存在返回 404；AI 不可用返回 200 + ``available=false`` 的降级结果。
    """
    record = get_review_record(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Review record not found.")

    payload = {
        "request_id": record.get("request_id", request_id),
        "doc_type": record.get("doc_type") or "bank_card",
        "review_result": record.get("review_result") or "",
        "quality_result": record.get("quality_result"),
        "quality_reasons": record.get("quality_reasons") or [],
        "review_reasons": record.get("review_reasons") or [],
        # fields 里含完整证件号，脱敏在 AIAssistClient 内部统一强制执行，
        # 这里不做二次处理，避免两处逻辑不一致。
        "fields": record.get("fields_json") or {},
        "error_message": record.get("error_message"),
        "ocr_mode": record.get("ocr_mode"),
    }

    result = _client().explain(payload)
    logger.info(
        "ai explain request_id=%s available=%s degraded=%s reason=%s",
        request_id,
        result.get("available"),
        result.get("degraded"),
        result.get("reason"),
    )
    return result
