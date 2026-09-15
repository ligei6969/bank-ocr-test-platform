"""Prompt 注册表的测试。

要点：id 必须稳定、版本必须显式、渲染必须显式失败（缺占位符不静默）、
以及 **只有真正调用过的 prompt 才进版本回传**。
"""

from __future__ import annotations

import pytest

from ai_service.prompts import (
    EXPLAIN_GENERATE,
    PROMPT_REGISTRY_VERSION,
    QUERY_REWRITE,
    RERANK,
    collect_labels,
    get_prompt,
    label_of,
    registry,
)


# ── 注册表完整性 ──────────────────────────────────────────────────────────────

#: 服务实际依赖的 prompt。新增 prompt 时把它加进来即可，
#: 断言写成「必需项必须都在」而不是「集合恰好等于」，避免加一条 prompt 就要改测试。
REQUIRED_PROMPTS = frozenset(
    {"query_rewrite", "rerank", "explain_generate", "agent_decide", "judge_rubric"}
)


def test_registry_contains_the_prompts_the_service_uses() -> None:
    assert REQUIRED_PROMPTS <= set(registry())


def test_prompt_ids_are_unique_and_match_their_keys() -> None:
    for key, prompt in registry().items():
        assert prompt.id == key


def test_every_prompt_has_a_version_and_a_purpose() -> None:
    for prompt in registry().values():
        assert prompt.version
        assert prompt.purpose
        # 版本号要能进 label，不能带 @
        assert "@" not in prompt.version
        assert "@" not in prompt.id


def test_label_is_id_at_version() -> None:
    assert QUERY_REWRITE.label == "query_rewrite@v1"
    assert label_of("rerank") == RERANK.label


def test_registry_version_is_dotted() -> None:
    assert PROMPT_REGISTRY_VERSION.count(".") == 1


def test_unknown_prompt_id_raises_with_the_known_ones_listed() -> None:
    with pytest.raises(KeyError) as excinfo:
        get_prompt("not_a_prompt")

    message = str(excinfo.value)
    assert "not_a_prompt" in message
    assert "explain_generate" in message


# ── 渲染 ──────────────────────────────────────────────────────────────────────

def test_render_substitutes_placeholders() -> None:
    text = QUERY_REWRITE.render(n=3, query="卡号缺失")

    assert "3 个不同角度" in text
    assert '"卡号缺失"' in text


def test_render_keeps_the_keywords_the_pipeline_relies_on() -> None:
    """检索链路靠「改写」/「排序」这类词区分用途，改 prompt 时不能弄丢。"""
    assert "改写" in QUERY_REWRITE.render(n=3, query="x")
    assert "排序" in RERANK.render(query="x", listing="1. y")


def test_render_missing_placeholder_fails_loudly() -> None:
    with pytest.raises(KeyError):
        QUERY_REWRITE.render(n=3)


def test_explain_prompt_carries_the_fact_constraint_in_system() -> None:
    # 「事实与措辞分离」是靠这段 system 约束模型的，属于安全边界，不能丢
    assert "不得编造" in EXPLAIN_GENERATE.system
    assert "事实条目" in EXPLAIN_GENERATE.render(
        question="q",
        doc_label="银行卡",
        result_label="待人工复核",
        quality_result="review",
        error_message="无",
        facts="[]",
        knowledge="（无）",
    )


# ── 版本回传 ──────────────────────────────────────────────────────────────────

def test_collect_labels_returns_only_prompts_actually_used() -> None:
    trace = [
        {"step": "rewrite", "strategy": "rule"},           # 降级，不带 prompt
        {"step": "rerank", "strategy": "llm", "prompt": "rerank@v1"},
        {"step": "generate", "engine": "llm", "prompt": "explain_generate@v1"},
    ]

    assert collect_labels(trace) == {
        "rerank": "rerank@v1",
        "explain_generate": "explain_generate@v1",
    }


def test_collect_labels_ignores_malformed_entries() -> None:
    trace = ["not a dict", {"prompt": 42}, {"prompt": "no-version"}, {"prompt": "a@b"}]

    assert collect_labels(trace) == {"a": "a@b"}


def test_collect_labels_tolerates_a_missing_trace() -> None:
    for value in (None, "text", 42):
        assert collect_labels(value) == {}
