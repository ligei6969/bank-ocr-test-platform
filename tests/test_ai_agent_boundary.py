"""平台 → AI 服务 → 外部模型：端到端的数据边界。

Agent 会读记录文本并据此决策，所以「模型到底看到了什么」是一条合规关键路径。
这条链路上有两个出网点：

1. **平台 → AI 服务**（HTTP 请求体）—— 由 ``app/ai_client.py`` 的脱敏保证；
2. **AI 服务 → 外部模型**（prompt）—— 依赖第 1 点，自己不脱敏。

本文件端到端验证这两跳：**一条含完整卡号、姓名、住址、身份证号的记录，
从平台发出去，一路走到 Agent 的模型输入，全程都不能出现原始值。**

为什么第二跳不自己再脱敏
------------------------
两边各写一份脱敏规则就一定会漂移，而漂移的那一份会静默失效 ——
比只有一份更危险。真正该守的是「入参必须已脱敏」这个契约，
所以这里把它测成契约，而不是在 AI 服务里复制一遍正则。
（如果哪天 AI 服务要开放给不受信任的调用方，那就必须补自己的脱敏层 ——
届时本测试的注释会提醒这一点。）
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from ai_service.agent import run_agent_for_context
from ai_service.explain import ReviewContext
from app.ai_client import AIAssistClient

from tests.test_ai_client import always, install_fake_urlopen  # noqa: E402

RAW_CARD = "6222021234567890"
RAW_ID = "110101199003071234"
RAW_NAME = "张三"
RAW_ADDRESS = "北京市朝阳区某某路 1 号"
RAW_ERROR = f"rejected card {RAW_CARD}"

SECRETS = (RAW_CARD, RAW_ID, RAW_NAME, RAW_ADDRESS)


class PromptRecorder:
    """假 planner：把收到的 prompt 全存下来，供边界断言检查。"""

    def __init__(self) -> None:
        self.prompts: List[str] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:recorder"

    async def complete(self, prompt: str, **_: Any) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "thought": "先看记录再去查释义",
                "action": "search_knowledge",
                "params": {"query": "image_blur 释义", "top_k": 5},
                "answer": "",
            },
            ensure_ascii=False,
        )


def platform_payload() -> Dict[str, Any]:
    """平台侧 ``/ai/explain`` 会构造的原始 payload（含未脱敏值）。"""
    return {
        "request_id": "req-boundary-1",
        "doc_type": "bank_card",
        "review_result": "review",
        "quality_result": "review",
        "quality_reasons": ["image_blur"],
        "review_reasons": ["missing_valid_date", "image_blur"],
        "fields": {
            "card_number": RAW_CARD,
            "valid_date": "08/29",
            "name": RAW_NAME,
            "id_number": RAW_ID,
            "address": RAW_ADDRESS,
        },
        "error_message": RAW_ERROR,
        "question": f"卡号 {RAW_CARD} 是什么问题？",
    }


def test_platform_egress_masks_every_sensitive_field(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_fake_urlopen(monkeypatch, always({"explanation": "ok"}))
    AIAssistClient().explain(platform_payload())

    sent = json.dumps(calls[0]["payload"], ensure_ascii=False)

    for secret in SECRETS:
        assert secret not in sent, f"未脱敏值被发出去了：{secret}"


def test_agent_never_sees_raw_values_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一跳脱敏后，把同一个 payload 交给 Agent，检查模型输入。"""
    calls = install_fake_urlopen(monkeypatch, always({"explanation": "ok"}))
    AIAssistClient().explain(platform_payload())
    sanitized = calls[0]["payload"]

    context = ReviewContext.from_payload(sanitized)
    planner = PromptRecorder()
    import asyncio

    result = asyncio.run(run_agent_for_context(context, llm=planner))

    # 模型侧的所有输入（每一步的 prompt）
    joined_prompts = "\n".join(planner.prompts)
    assert planner.prompts, "planner 一次都没被调用，测试没测到东西"
    for secret in SECRETS:
        assert secret not in joined_prompts, f"未脱敏值进了模型输入：{secret}"

    # 整个响应（含 trace、引用、答复）也不能带出来
    serialised = json.dumps(result, ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in serialised, f"未脱敏值进了 AI 服务的响应：{secret}"


def test_masked_values_are_still_useful_to_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """脱敏不能脱成一个用不了的字符串 —— 审核员还要靠它判断。"""
    calls = install_fake_urlopen(monkeypatch, always({"explanation": "ok"}))
    AIAssistClient().explain(platform_payload())
    sanitized = calls[0]["payload"]

    fields = sanitized["fields"]

    assert fields["card_number"].startswith("622202")
    assert fields["card_number"].endswith("7890")
    assert "*" in fields["card_number"]
    # 只动了敏感字段，非敏感字段原样保留
    assert fields["valid_date"] == "08/29"


def test_agent_tool_catalogue_is_not_a_data_channel() -> None:
    """工具清单进 prompt，但它只能描述能力，不能夹带记录内容。"""
    from ai_service.tools import tool_catalog_text

    catalogue = tool_catalog_text()

    for secret in SECRETS:
        assert secret not in catalogue
