"""AI 复核助手的 HTTP 接口。

对外只暴露三个端点，都很窄：

* ``GET  /health``  —— 平台侧探测用，报告 LLM 是否可用与索引规模
* ``POST /explain`` —— 主接口，传入脱敏后的审核上下文，返回解释与处置建议
* ``POST /search``  —— 检索调试用，方便单独验证召回质量

平台侧调用统一走 ``app/ai_client.py``，带超时、熔断和降级；
本服务不反向依赖平台，也不访问平台数据库（记录由平台取好并脱敏后传入）。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ai_service import __version__
from ai_service.explain import ReviewContext, ReviewExplainer, build_explainer
from ai_service.llm import build_llm_client

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8100
MAX_TOP_K = 20


class ExplainRequest(BaseModel):
    """平台侧传入的审核上下文。字段值应已由平台完成脱敏。"""

    request_id: str = Field(..., description="审核请求 ID")
    doc_type: str = Field(default="bank_card", description="bank_card 或 id_card")
    review_result: str = Field(default="", description="pass / review / reject / error")
    quality_result: Optional[str] = None
    quality_reasons: List[str] = Field(default_factory=list)
    review_reasons: List[str] = Field(default_factory=list)
    fields: Dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None
    ocr_mode: Optional[str] = None
    question: str = Field(default="", description="审核员的自由提问，留空则用默认问题")
    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K)
    reason_codes: List[str] = Field(default_factory=list)
    doc_type: Optional[str] = None


def create_app(explainer: Optional[ReviewExplainer] = None) -> FastAPI:
    """构造 FastAPI 应用。允许注入解释器，便于测试替换检索/LLM。"""
    app = FastAPI(
        title="Bank OCR AI Review Assistant",
        version=__version__,
        description="审核原因码解释与处置建议服务（P0）",
    )

    active_explainer = explainer or build_explainer(llm=build_llm_client())

    @app.get("/health")
    def health() -> Dict[str, Any]:
        llm = active_explainer._llm  # noqa: SLF001 - 同包内读取，保持单一实例
        return {
            "status": "ok",
            "version": __version__,
            "llm_available": llm.available,
            "llm": llm.name,
            "doc_count": active_explainer._retriever.doc_count,  # noqa: SLF001
            "chunk_count": active_explainer._retriever.chunk_count,  # noqa: SLF001
        }

    @app.get("/tools/stats")
    def tool_stats() -> Dict[str, Any]:
        return active_explainer._tools.get_stats()  # noqa: SLF001

    @app.post("/explain")
    async def explain(payload: ExplainRequest) -> Dict[str, Any]:
        if payload.doc_type not in {"bank_card", "id_card"}:
            raise HTTPException(status_code=422, detail="doc_type 必须是 bank_card 或 id_card")
        context = ReviewContext.from_payload(payload.model_dump())
        try:
            return await active_explainer.explain(context, top_k=payload.top_k)
        except Exception as exc:  # noqa: BLE001 - 服务边界统一兜底
            logger.exception("解释生成失败 request_id=%s", payload.request_id)
            raise HTTPException(status_code=500, detail=f"解释生成失败: {exc}") from exc

    @app.post("/search")
    def search(payload: SearchRequest) -> Dict[str, Any]:
        hits = active_explainer._retriever.search(  # noqa: SLF001
            payload.query,
            top_k=payload.top_k,
            reason_codes=payload.reason_codes,
            doc_type=payload.doc_type,
        )
        return {
            "query": payload.query,
            "count": len(hits),
            "hits": [hit.to_dict() for hit in hits],
        }

    return app


def get_host() -> str:
    return os.getenv("AI_SERVICE_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST


def get_port() -> int:
    raw = os.getenv("AI_SERVICE_PORT", "").strip()
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


app = create_app()
