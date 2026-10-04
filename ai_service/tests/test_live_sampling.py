"""多次采样评测框架的机制测试（离线，不需要 API key）。

这里测的是**机制**而不是模型质量：
- 同一指标多轮的均值 / 标准差 / 极差算得对不对；
- 单轮时 stdev 是 ``None`` 而不是 0（假装有精度）；
- 某轮缺的指标不进分布（平均掉它是在掩盖问题）；
- key 只走环境变量，任何结果文件里都不含 key。

真实模型质量由 ``scripts/evaluate_live_sampling.py --live`` 跑，
其结果在 reports/live-sampling/，不属于本文件的断言范围。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_service.eval.sampling import (
    MetricSpread,
    ModelConfig,
    RoundResult,
    build_sampling_report,
    compute_spreads,
)


def _round(index: int, flat: dict[str, float], model: str = "m") -> RoundResult:
    return RoundResult(
        round_index=index, model_name=model, flat=dict(flat), execution={}
    )


# ── 分布计算 ──────────────────────────────────────────────────────────────────

def test_spreads_compute_mean_stdev_and_range() -> None:
    """三轮的同一指标要给出均值、样本标准差和极差。"""
    results = [
        _round(0, {"tools.tool_set_accuracy": 0.8}),
        _round(1, {"tools.tool_set_accuracy": 1.0}),
        _round(2, {"tools.tool_set_accuracy": 0.9}),
    ]
    spread = compute_spreads(results)
    item = spread["tools.tool_set_accuracy"]
    assert item.mean == pytest.approx(0.9)
    assert item.stdev == pytest.approx(0.1)
    assert item.min == 0.8
    assert item.max == 1.0
    assert item.range == pytest.approx(0.2)
    assert len(item.values) == 3


def test_single_round_stdev_is_none_not_zero() -> None:
    """单轮没有自由度。

    输出 0 会让读者以为「方差为零 = 非常稳定」，而事实是「没测方差」。
    这是本框架存在的理由之一，必须是硬断言。
    """
    spread = compute_spreads([_round(0, {"x": 0.5})])
    assert spread["x"].stdev is None
    payload = spread["x"].to_dict()
    assert payload["stdev"] is None
    assert payload["n"] == 1


def test_metrics_missing_from_a_round_are_excluded() -> None:
    """一轮有一轮没有的指标不能进分布。

    平均掉它等于假设「缺的那轮也测了」，而实际是两轮跑的不是同一个东西。
    """
    results = [
        _round(0, {"a": 1.0, "b": 2.0}),
        _round(1, {"a": 1.0}),  # 这轮没有 b
    ]
    spread = compute_spreads(results)
    assert "a" in spread
    assert "b" not in spread


def test_no_common_metrics_yields_empty_spread() -> None:
    """两轮指标完全不相交时，分布为空而不是报错或硬造。"""
    results = [_round(0, {"a": 1.0}), _round(1, {"b": 2.0})]
    assert compute_spreads(results) == {}


def test_empty_results_yield_no_spreads() -> None:
    assert compute_spreads([]) == {}


# ── 序列化 ────────────────────────────────────────────────────────────────────

def test_report_serializes_with_sample_sizes_and_notes() -> None:
    """报告必须带 n 和说明 —— 没有 n 的均值是不可解读的。"""
    report = build_sampling_report(
        3,
        {"m": [_round(i, {"a": 0.5 + 0.1 * i}) for i in range(3)]},
    )
    payload = report.to_dict()
    item = payload["spreads"]["m"]["a"]
    assert item["n"] == 3
    assert item["stdev"] is not None
    assert payload["rounds"] == 3
    assert payload["notes"], "报告必须带诚实边界说明"


def test_json_payload_never_contains_an_api_key(tmp_path: Path) -> None:
    """key 只走环境变量 —— 序列化产物里不允许出现 key 片段。

    这是把「不落盘」从口头承诺变成可断言的契约。
    """
    secret = "sk-DO-NOT-LEAK-0000000000000000"
    config = ModelConfig(
        name="m", provider="openai", model="m", base_url="https://example.invalid"
    )
    # env() 是 key 唯一出现的地方；构造出的环境只用于 build_llm_client
    env = config.env(secret)
    assert env["LLM_API_KEY"] == secret

    report = build_sampling_report(
        1, {"m": [_round(0, {"a": 1.0})]}, notes=[f"模型配置：{config.name}"]
    )
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    assert secret not in payload, "API key 出现在了采样报告里"


# ── 模型配置 ──────────────────────────────────────────────────────────────────

def test_model_config_env_carries_base_url_only_when_set() -> None:
    """base_url 为空时不写入环境，让 llm.py 用官方默认 —— 避免写入空串覆盖。"""
    with_url = ModelConfig(name="a", provider="openai", model="m", base_url="https://x")
    without_url = ModelConfig(name="b", provider="openai", model="m", base_url="")
    assert with_url.env("k")["LLM_BASE_URL"] == "https://x"
    assert "LLM_BASE_URL" not in without_url.env("k")


def test_round_result_label_identifies_model_and_round() -> None:
    """每轮结果要能自报身份 —— 跨模型报告里分不清归属的数字没有意义。"""
    assert _round(2, {}, model="deepseek-flash").label == "deepseek-flash#r2"


# ── 预置模型与真实 API 的一致性（离线可测的部分）───────────────────────────────

def test_preset_model_names_match_the_live_api_catalogue() -> None:
    """预置模型名必须与 DeepSeek 实际开放的模型一致。

    2026-10-03 实测 GET /models 只有 deepseek-flash 与 deepseek-v4-pro；
    项目旧文档里的 deepseek-chat / deepseek-reasoner 已下线，
    用旧名会得到 400 而不是「模型不存在」，容易误判成配置问题。
    断言写成「不允许出现已下线的旧名」+「预置不为空」，而不是硬编码
    名单 —— 线上目录变了这里会提醒你去对，但不会误伤新模型。
    """
    from scripts.evaluate_live_sampling import PRESET_MODELS

    assert PRESET_MODELS, "至少要预置一个模型"
    retired = {"deepseek-chat", "deepseek-reasoner"}
    assert not (set(PRESET_MODELS) & retired), (
        f"预置里含已下线的模型：{set(PRESET_MODELS) & retired} —— "
        "它们在 2026-10-03 已不在 DeepSeek 目录里"
    )


def test_resolve_model_supports_custom_endpoint() -> None:
    """model@base_url 形式要能落到自定义端点 —— 跨 provider 的逃生口。"""
    from scripts.evaluate_live_sampling import _resolve_model

    config = _resolve_model("some-model@https://relay.example/v1")
    assert config.model == "some-model"
    assert config.base_url == "https://relay.example/v1"
    # 未知名默认按 DeepSeek 裸模型名处理
    bare = _resolve_model("deepseek-flash")
    assert bare.model == "deepseek-flash"
    assert "api.deepseek.com" in bare.base_url


# ── 诚实边界 ──────────────────────────────────────────────────────────────────

def test_report_notes_state_the_limits_of_small_n() -> None:
    """说明里必须写清 n<3 只作参考 —— 不写，读者会把噪声当结论。"""
    report = build_sampling_report(1, {"m": [_round(0, {"a": 1.0})]})
    joined = "\n".join(report.notes)
    assert "n<3" in joined
    assert "单轮采样无自由度" in joined
