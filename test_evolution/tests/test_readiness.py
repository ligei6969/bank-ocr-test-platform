"""Surface Readiness 的测试。

守的是那条容易走偏的判断：**数据缺口只该卡住它真正污染的那两个面。**
如果哪天有人把闸门收紧成「全系统阻塞」，这些测试会红 ——
那正是 CTE-1 能先跑起来的原因，不该被无声地撤销。
"""

from __future__ import annotations

import pytest

from test_evolution import readiness
from test_evolution.readiness import BLOCKED, PARTIAL, READY, learning_allowed, surface
from test_evolution.schema import BLOCKED_SURFACES, EVENT_SURFACES, Event


def test_every_event_surface_has_a_readiness_entry():
    """有 surface 但没就绪度 = 那条线上没人知道能不能做演进。"""
    for name in EVENT_SURFACES:
        assert surface(name).name == name


def test_data_gap_blocks_only_the_surfaces_it_actually_pollutes():
    """缺口是「真实 OCR 字段」，它污染 OCR 与双判 —— 不该连知识面一起停。"""
    assert learning_allowed("knowledge") is True
    assert learning_allowed("threat") is True
    assert learning_allowed("ocr") is False
    assert learning_allowed("adjudication") is False


def test_agent_is_partial_not_ready():
    """agent 面有可用的判据也有已被证伪的判据，不能标成 ready。"""
    item = surface("agent")

    assert item.level == PARTIAL
    assert learning_allowed("agent") is True
    assert "tool_set_accuracy" in item.why or "工具序列" in item.why


def test_blocked_surfaces_declare_an_exit_condition():
    """卡住不可怕，说不清怎么解除才可怕。"""
    for name in EVENT_SURFACES:
        item = surface(name)
        if item.level == BLOCKED:
            assert item.exit_condition, f"{name} 被标为 blocked 却没写解除条件"


def test_ready_surfaces_do_not_claim_an_exit_condition():
    """已经能做的面不该写「解除条件」，那会让人以为它还没开始。"""
    for name in EVENT_SURFACES:
        item = surface(name)
        if item.level == READY:
            assert not item.exit_condition, name


def test_readiness_is_the_single_source_for_learning_blocked():
    """schema 的 learning_blocked 必须委托给就绪度表，不能各说各话。"""
    for name in EVENT_SURFACES:
        event = Event(
            event_id="E", source="new_bug", surface=name, title="t",
            observed_at="", system_version="", input_case="",
            current_result="", expected_result="",
        )
        assert event.learning_blocked is (not learning_allowed(name)), name


def test_blocked_surfaces_constant_agrees_with_the_table():
    assert BLOCKED_SURFACES == {s.name for s in readiness.SURFACES if s.level == BLOCKED}


def test_readiness_table_renders():
    table = readiness.readiness_table()

    assert "| Surface |" in table
    for name in EVENT_SURFACES:
        assert f"`{name}`" in table


def test_unknown_surface_fails_loudly():
    with pytest.raises(KeyError, match="未知 surface"):
        surface("不存在面")


@pytest.mark.parametrize("name", ["ocr", "adjudication"])
def test_blocked_surfaces_explain_why_not_just_that(name: str) -> None:
    """「blocked」必须带理由 —— 否则它会变成一个没人敢动的黑盒。"""
    item = surface(name)

    assert item.level == BLOCKED
    assert len(item.why) > 40, f"{name} 的 blocked 理由太短，说不清问题"
