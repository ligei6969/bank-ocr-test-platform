"""token 用量协议与 Agent 记账。

这里验的是 P1.5 的核心契约：**真实用量优先，拿不到才估**。
两者必须可分辨 —— 把估算值当成成本结论，等于给「成本可控」注水，
而这种错误在报告里是看不出来的（数字都在，只是来源不同）。
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

from ai_service.agentkit import ScriptedPlanner, blurred_context, decision, make_agent, run
from ai_service.llm import (
    USAGE_ESTIMATE,
    USAGE_PROVIDER,
    LLMUsage,
    NullLLMClient,
    take_usage,
)
from ai_service.tools import ContextRecordSource


# ── 假模型 ────────────────────────────────────────────────────────────────────

def _decision(action: str, **params: Any) -> str:
    return json.dumps(
        {"thought": "下一步", "action": action, "params": params}, ensure_ascii=False
    )


def _finish(answer: str = "已给出解释。") -> str:
    return json.dumps(
        {"thought": "信息够了", "action": "finish", "params": {}, "answer": answer},
        ensure_ascii=False,
    )


class UsageLLM:
    """会报告真实用量的假模型。"""

    def __init__(
        self,
        replies: List[str],
        *,
        prompt_tokens: int = 100,
        completion_tokens: int = 20,
    ) -> None:
        self._replies = list(replies)
        self._pending: Optional[LLMUsage] = None
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls: List[str] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:fake-usage"

    async def complete(self, prompt: str, **_: Any) -> str:
        self.calls.append(prompt)
        text = self._replies.pop(0) if self._replies else "兜底答复。"
        # 累加而不是覆盖：一次业务请求可能调好几次模型
        usage = LLMUsage(
            self.prompt_tokens, self.completion_tokens, source=USAGE_PROVIDER
        )
        self._pending = usage if self._pending is None else self._pending + usage
        return text

    def take_usage(self) -> Optional[LLMUsage]:
        usage, self._pending = self._pending, None
        return usage


class HalfReportingLLM:
    """只在上半场报用量：用来验证混用时的来源标记。"""

    def __init__(self, replies: List[str]) -> None:
        self._replies = list(replies)
        self._pending: Optional[LLMUsage] = None
        self.reads = 0

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:fake-half-meter"

    async def complete(self, prompt: str, **_: Any) -> str:
        text = self._replies.pop(0) if self._replies else "兜底答复。"
        if self.reads == 0:
            self._pending = LLMUsage(50, 10, source=USAGE_PROVIDER)
        return text

    def take_usage(self) -> Optional[LLMUsage]:
        usage, self._pending = self._pending, None
        self.reads += 1
        return usage


def _agent_outcome(llm: Any) -> dict:
    context = blurred_context()
    agent = make_agent(
        llm,
        context=context,
        records=ContextRecordSource(
            request_id=context.request_id, record=context.to_record_fields()
        ),
    )
    return run(agent.run(context)).to_dict()


# ── 用量对象与取用语义 ────────────────────────────────────────────────────────

def test_take_usage_returns_none_when_the_client_has_no_meter() -> None:
    """没实现 take_usage 的客户端（大量测试替身）必须被容忍。"""
    assert take_usage(ScriptedPlanner([decision("finish", answer="好。")])) is None
    assert take_usage(object()) is None


def test_take_usage_never_raises_when_the_meter_is_broken() -> None:
    """用量是旁路信息，计量器坏了不该把主链路带崩。"""

    class Broken:
        def take_usage(self) -> None:
            raise RuntimeError("meter exploded")

    assert take_usage(Broken()) is None


def test_null_client_reports_a_real_zero_not_a_missing_value() -> None:
    """没模型时成本**确实是 0**，这是真实值，不是「拿不到」。"""
    usage = take_usage(NullLLMClient())

    assert usage is not None
    assert usage.total == 0
    assert usage.source == USAGE_PROVIDER
    assert usage.estimated is False


def test_adding_usage_degrades_the_label_when_any_part_is_estimated() -> None:
    """只要混进一次估算，合计就不许再自称真实值。"""
    real = LLMUsage(10, 5, source=USAGE_PROVIDER)
    guessed = LLMUsage(3, 2, source=USAGE_ESTIMATE)

    assert (real + guessed).total == 20
    assert (real + guessed).source == USAGE_ESTIMATE
    assert (real + guessed).estimated is True
    assert (real + real).source == USAGE_PROVIDER


def test_a_single_read_returns_the_sum_of_several_calls() -> None:
    """累计语义：一次取走拿到的是多轮调用之和，而不是最后一次。"""
    llm = UsageLLM(["a", "b", "c"], prompt_tokens=10, completion_tokens=1)

    run(llm.complete("p1"))
    run(llm.complete("p2"))
    usage = take_usage(llm)

    assert usage is not None
    assert usage.total == (10 + 1) * 2


def test_reading_drains_so_the_same_call_is_not_counted_twice() -> None:
    llm = UsageLLM(["a"], prompt_tokens=7, completion_tokens=3)

    run(llm.complete("p1"))

    assert take_usage(llm).total == 10
    assert take_usage(llm) is None


# ── Agent 记账 ────────────────────────────────────────────────────────────────

def test_agent_prefers_provider_usage_over_estimation() -> None:
    llm = UsageLLM([_finish()], prompt_tokens=120, completion_tokens=30)

    outcome = _agent_outcome(llm)
    usage = outcome["token_usage"]

    assert usage["source"] == USAGE_PROVIDER
    assert usage["prompt_tokens"] == 120
    assert usage["completion_tokens"] == 30
    assert usage["provider_total"] == 150
    assert usage["estimated_tokens"] == 0
    assert usage["total"] == 150
    assert usage["llm_calls"] == 1
    # 预算用的是同一个合计值：预算与报告不该有两套口径
    assert outcome["budget_used"]["tokens"] == 150


def test_agent_falls_back_to_estimation_when_the_provider_hides_usage() -> None:
    planner = ScriptedPlanner([decision("finish", answer="好。")])

    outcome = _agent_outcome(planner)
    usage = outcome["token_usage"]

    assert usage["source"] == USAGE_ESTIMATE
    assert usage["provider_total"] == 0
    assert usage["estimated_tokens"] > 0
    assert usage["total"] == usage["estimated_tokens"]
    assert usage["llm_calls"] == 1
    assert outcome["budget_used"]["tokens"] == usage["total"]


def test_a_partially_reporting_provider_is_labelled_mixed() -> None:
    llm = HalfReportingLLM(
        [_decision("search_knowledge", query="模糊 释义", top_k=3), _finish()]
    )

    usage = _agent_outcome(llm)["token_usage"]

    assert usage["source"] == "mixed"
    assert usage["provider_total"] == 60
    assert usage["estimated_tokens"] > 0
    assert usage["total"] == usage["provider_total"] + usage["estimated_tokens"]


def test_an_offline_run_reports_no_model_cost() -> None:
    """离线路径不调模型，成本就是 0 —— 标 none 而不是 estimate，避免误导。"""
    outcome = _agent_outcome(NullLLMClient())

    usage = outcome["token_usage"]

    assert usage["source"] == "none"
    assert usage["total"] == 0
    assert usage["llm_calls"] == 0
    assert outcome["engine"]["decision"] == "rule"


def test_a_failed_decision_still_counts_the_call_it_paid_for() -> None:
    """模型回了不可解析的内容 —— 钱已经花了，成本必须计进去。

    这是「成本漏记」最容易发生的地方：调用抛异常或解析失败时，
    实现很容易只记成功的那次，于是「模型不可靠」的代价在报告里看不见。
    """

    class GarbageLLM:
        @property
        def available(self) -> bool:
            return True

        @property
        def name(self) -> str:
            return "llm:garbage"

        async def complete(self, prompt: str, **_: Any) -> str:
            return "我觉得应该重拍。"  # 散文，不带 JSON 结构

    outcome = _agent_outcome(GarbageLLM())
    usage = outcome["token_usage"]

    assert outcome["engine"]["decision"] == "rule"  # 已降级
    assert usage["llm_calls"] == 1  # 但那次调用确实发生了
    assert usage["source"] == USAGE_ESTIMATE
    assert usage["estimated_tokens"] > 0


def test_token_usage_is_exposed_on_every_path() -> None:
    """两条路径的输出结构必须一致，否则前端要为「降级」多写一套分支。"""
    for llm in (NullLLMClient(), ScriptedPlanner([decision("finish", answer="好。")])):
        outcome = _agent_outcome(llm)

        assert "token_usage" in outcome
        assert set(outcome["token_usage"]) == {
            "prompt_tokens",
            "completion_tokens",
            "provider_total",
            "estimated_tokens",
            "total",
            "llm_calls",
            "source",
        }
