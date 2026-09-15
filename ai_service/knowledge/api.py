"""客服 Agent 的 HTTP 接口（挂载在同一个服务进程内）。

路径刻意分成两个前缀：

* ``/explain``、``/agent/explain`` —— 审核侧（面向审核员，处理一条记录）
* ``/knowledge/ask``              —— 客服侧（面向用户/客户经理，回答一个问题）

两者的服务对象、安全边界、可答范围都不同，**不该共用一个入口**。
合在一起意味着「权限相同、话术相同」，而实际上客服侧必须比审核侧更保守：
审核员看得到原因码，用户看不到。

只暴露一个端点是有意的：越窄的接口越难被误用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ai_service.knowledge.agent import KnowledgeAgent, build_knowledge_agent
from ai_service.llm import LLMClient

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1000


class AskRequest(BaseModel):
    """客服提问。``context`` 预留给多轮会话，当前不参与决策。"""

    question: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUESTION_CHARS,
        description="用户的业务问题，例如「办理二类账户需要哪些材料」",
    )
    context: Dict[str, Any] = Field(
        default_factory=dict,
        description="预留：多轮会话上下文（当前版本不使用）",
    )


def create_knowledge_router(
    agent: Optional[KnowledgeAgent] = None,
    *,
    llm: Optional[LLMClient] = None,
) -> APIRouter:
    """构造客服路由。

    与 ``create_app`` 一致：**eager 构造** Agent 与索引。语料只有十几条，
    建索引的成本远小于「第一次请求慢一拍」带来的困惑。
    """
    router = APIRouter(prefix="/knowledge", tags=["knowledge"])
    active = agent or build_knowledge_agent(llm=llm)

    @router.get("/health")
    def health() -> Dict[str, Any]:
        llm_client = active._llm  # noqa: SLF001 - 同包内读取，保持单一实例
        return {
            "status": "ok",
            "surface": "knowledge",
            "llm_available": llm_client.available,
            "llm": llm_client.name,
            "retrieval": active._retrieval_backend,  # noqa: SLF001
        }

    @router.post("/ask")
    async def ask(payload: AskRequest) -> Dict[str, Any]:
        """回答一个业务知识问题。

        三种正常结局，都是 200：

        * 答出来了（``citations`` 有依据）；
        * 越界拒答（``refused=true``，附合规口径与转人工信息）；
        * 答不出（``stop_reason=ungrounded``，附人工受理回执）。

        「答不出」不是错误状态，把它做成 4xx/5xx 会诱导调用方重试，
        而重试同一个问题也不会变得答得出来。真正的错误只有一类：
        服务内部异常（500）。
        """
        try:
            # 必须转成 dict：路由声明的返回类型是 JSON 对象，
            # 直接把 dataclass 返回去会被 FastAPI 的响应校验拦下（500）
            return (await active.ask(payload.question)).to_dict()
        except Exception as exc:  # noqa: BLE001 - 服务边界统一兜底
            logger.exception("客服问答失败 question_len=%s", len(payload.question))
            raise HTTPException(status_code=500, detail=f"客服问答失败: {exc}") from exc

    return router


__all__ = ("MAX_QUESTION_CHARS", "AskRequest", "create_knowledge_router")
