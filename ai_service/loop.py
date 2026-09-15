"""两个 Agent surface 共用的循环机制。

抽出来的东西与**没抽**的东西
----------------------------
抽的是**机制**：

* 文本截断（trace 与 prompt 都要用，不截断上下文会爆）
* 观察结果摘要（把工具返回压成一句话）
* token 记账（真实用量优先、字符估算兜底）

**没抽**的是策略：什么时候转人工、什么算越界、证据够了没有 ——
这些两个 surface 的答案完全不同，硬抽成一个模板方法只会得到一堆
``if surface == "review"`` 的分支，比重复更糟。

边界怎么定的：**机制出错是 bug，策略出错是产品决定**。
前者应该只有一份实现，后者应该各写各的、各自有测试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ai_service.llm import (
    USAGE_ESTIMATE,
    USAGE_PROVIDER,
    LLMClient,
    LLMUsage,
    take_usage,
)

DEFAULT_OBSERVATION_CHARS = 400


def estimate_tokens(text: str) -> int:
    """字符级 token 估算，用于预算兜底。

    CJK 大致 1 字 1 token，拉丁文约 4 字符 1 token。不追求准确 ——
    它只需要「随输出长度单调增长」，好让预算能真的封顶。
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(0, other) // 4


def clip(text: Any, limit: int) -> str:
    """截断长文本。trace 与 prompt 都走它，避免单条观察把上下文撑爆。"""
    value = "" if text is None else str(text)
    value = value.replace("\n", " ").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def summarize_observation(data: Any, limit: int = DEFAULT_OBSERVATION_CHARS) -> str:
    """把工具返回压成一句可读摘要。

    只做通用处理（列表报条数与标题，字典转 JSON）。带领域语义的摘要
    （比如「记录不存在」这种要翻译成人话的分支）留在各自的 surface 里，
    因为它们说的是业务，不是机制。
    """
    if isinstance(data, list):
        titles = [
            str(item.get("title") or item.get("doc_id") or "")
            for item in data[:4]
            if isinstance(item, dict)
        ]
        head = "、".join(title for title in titles if title)
        return clip(f"{len(data)} 条结果：{head}", limit)
    if isinstance(data, dict):
        return clip(json.dumps(data, ensure_ascii=False, default=str), limit)
    return clip(data, limit)


@dataclass
class TokenLedger:
    """一次请求的 token 账本。

    预算用「真实 or 估算」的合计值封顶 —— 预算的目的是**封顶**，
    混用两种口径不会让它失效，反而比「拿不到 usage 就不计费」更安全：
    后者会让不返回 usage 的 provider 变成预算黑洞。

    报告则把两者**分开呈现**（:meth:`report`），避免估算值冒充账单。
    """

    tokens: int = 0
    llm_calls: int = 0
    unreported_calls: int = 0
    estimated_tokens: int = 0
    provider_usage: LLMUsage = field(default_factory=lambda: LLMUsage(source=USAGE_PROVIDER))

    def charge(self, llm: LLMClient, prompt: str, raw: str) -> None:
        """给一次模型调用记账。

        **失败与解析失败的调用同样要计** —— 钱已经花了。这是成本最容易漏记的
        地方：实现很容易只记成功的那次，于是「模型不可靠」的代价在报告里看不见。
        """
        self.llm_calls += 1
        usage = take_usage(llm)
        if usage is None:
            self.unreported_calls += 1
            spent = estimate_tokens(prompt) + estimate_tokens(raw)
            self.estimated_tokens += spent
        else:
            self.provider_usage = self.provider_usage + usage
            spent = usage.total
        self.tokens += spent

    def take(self, llm: LLMClient) -> Optional[LLMUsage]:
        """取走客户端侧尚未计入的用量。用于不走 ``charge`` 的调用路径。"""
        usage = take_usage(llm)
        if usage is None:
            return None
        self.llm_calls += 1
        self.provider_usage = self.provider_usage + usage
        self.tokens += usage.total
        return usage

    @property
    def source(self) -> str:
        """这次记账的可信度标签。

        * ``none``     没调用模型（确定性路径），成本确实是 0
        * ``provider`` 全部调用都有 provider 回传，可作计费依据
        * ``estimate`` 全部调用都拿不到 usage，纯估算
        * ``mixed``    一部分有一部没有 —— 合计值不能当账单，只能说个量级
        """
        if self.llm_calls == 0:
            return "none"
        if self.unreported_calls == 0:
            return USAGE_PROVIDER
        if self.unreported_calls >= self.llm_calls:
            return USAGE_ESTIMATE
        return "mixed"

    def report(self) -> Dict[str, Any]:
        real = self.provider_usage
        return {
            "prompt_tokens": real.prompt_tokens,
            "completion_tokens": real.completion_tokens,
            "provider_total": real.total,
            "estimated_tokens": self.estimated_tokens,
            "total": self.tokens,
            "llm_calls": self.llm_calls,
            "source": self.source,
        }

    def budget_used(self) -> Dict[str, Any]:
        return {"tokens": self.tokens}


__all__ = (
    "DEFAULT_OBSERVATION_CHARS",
    "TokenLedger",
    "clip",
    "estimate_tokens",
    "summarize_observation",
)
