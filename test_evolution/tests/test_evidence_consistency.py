"""CTE 证据树的自一致性检查。

为什么需要这个
--------------
验收时最容易出问题的地方不是「功能没做完」，而是**做完的事在记录上没对齐**：

* 代码里修好了，候选记录还停在 `machine_validated`（审计轨迹说「提案中」）
* 文档写着「已修复、已验证」，而 `validated/` 里没有那份文件
* `EVENT_LOG` 里注册了事件，磁盘上没有对应的事件/复盘文件

这些都出现过（CTE 收口时一次修掉三处）。共同点是：**没有一处机制在核对
「说的」与「做的」是否一致** —— 全靠人记得。

本文件把几条可机械核对的规律固化成断言。它不能覆盖全部表述，
但能挡住最容易发生、后果最直接的那几类不一致。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 事件注册表所在的模块。
from test_evolution.pipeline import EVENT_LOG, RECORD_ONLY  # noqa: E402
from test_evolution.schema import DEFAULT_EVOLUTION_DIR  # noqa: E402


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 事件与产物一一对应 ────────────────────────────────────────────────────────

def test_every_registered_event_has_an_event_file_on_disk():
    """注册进 ``EVENT_LOG`` 的事件必须真的跑过 —— 磁盘上要有事件文件。

    `--list` 读的是代码里的注册表，所以光注册就能列出来。
    但没有文件意味着它从没被执行过：注册表与产物脱节。
    （EVT-006 就是这样：注册了，但没跑，目录里没有它。）
    """
    registered = [spec["event_id"] for spec in EVENT_LOG]
    events_dir = DEFAULT_EVOLUTION_DIR / "events"

    missing = [
        event_id
        for event_id in registered
        if not (events_dir / f"{event_id}.json").is_file()
    ]
    assert not missing, (
        f"这些事件已注册但没有事件文件（跑一次 run_cte --event <id>）：{missing}"
    )


def test_every_event_file_is_registered_in_the_log():
    """反向也要成立：磁盘上有的事件必须能在注册表里找到。

    否则 ``--list`` 会漏掉它，而它其实是存在的。
    """
    registered = {spec["event_id"] for spec in EVENT_LOG}
    events_dir = DEFAULT_EVOLUTION_DIR / "events"
    if not events_dir.is_dir():
        pytest.skip("没有事件文件")

    orphaned = [
        path.stem
        for path in sorted(events_dir.glob("EVT-*.json"))
        if path.stem not in registered
    ]
    assert not orphaned, f"这些事件文件没有在 EVENT_LOG 里登记：{orphaned}"


def test_every_event_has_a_prediction():
    """盲预测是 CTE 的核心不变量：每个事件都必须在执行前留下预测。"""
    events_dir = DEFAULT_EVOLUTION_DIR / "events"
    predictions_dir = DEFAULT_EVOLUTION_DIR / "predictions"
    if not events_dir.is_dir():
        pytest.skip("没有事件")

    missing = [
        path.stem
        for path in sorted(events_dir.glob("EVT-*.json"))
        if not (predictions_dir / f"PRED-{path.stem}.json").is_file()
    ]
    assert not missing, f"这些事件缺少盲预测（等于放弃了「不许事后改口」）：{missing}"


# ── validated/ 与署名一一对应 ─────────────────────────────────────────────────

def test_validated_assets_are_exactly_the_signed_candidates():
    """``validated/`` 的内容必须**恰好**是已署名晋级的候选。

    这条守的是两个方向：

    * 有文件但候选没签名 —— 那是绕过人工门；
    * 候选签了名但没文件 —— 那是审计轨迹说「晋级了」而资产不存在。
    """
    candidates_dir = DEFAULT_EVOLUTION_DIR / "candidates"
    validated_dir = DEFAULT_EVOLUTION_DIR / "validated"

    signed = set()
    if candidates_dir.is_dir():
        for path in sorted(candidates_dir.glob("CTE-*.json")):
            payload = _read_json(path)
            if payload.get("status") == "validated":
                assert payload.get("approver"), (
                    f"{path.stem} 状态是 validated 却没有署名 —— "
                    "晋级必须有人批准"
                )
                signed.add(path.stem)

    on_disk = (
        {path.stem for path in validated_dir.glob("CTE-*.md")}
        if validated_dir.is_dir()
        else set()
    )

    assert on_disk == signed, (
        f"validated/ 与已署名候选不一致：\n"
        f"  只在磁盘上（没签名的资产）：{sorted(on_disk - signed)}\n"
        f"  只在候选里（签了名但无文件）：{sorted(signed - on_disk)}"
    )


def test_validated_assets_name_their_approver():
    """晋级的资产正文里必须写明批准人 —— 它要能被追溯。"""
    validated_dir = DEFAULT_EVOLUTION_DIR / "validated"
    if not validated_dir.is_dir():
        pytest.skip("没有晋级资产")

    for path in sorted(validated_dir.glob("CTE-*.md")):
        body = path.read_text(encoding="utf-8")
        assert "批准人" in body, f"{path.name} 没有写明批准人"
        assert not re.search(r"批准人\s*$", body, re.MULTILINE), (
            f"{path.name} 的批准人是空的"
        )


# ── 「有意不产出候选」与「忘了配」要分开 ──────────────────────────────────────

def test_record_only_events_have_no_candidate():
    """``RECORD_ONLY`` 里的事件不该有候选文件。

    有意不产出候选是判断；产出了候选又声称「有意不产出」则是自相矛盾。
    """
    candidates_dir = DEFAULT_EVOLUTION_DIR / "candidates"
    if not candidates_dir.is_dir():
        pytest.skip("没有候选")

    existing = {path.stem for path in candidates_dir.glob("CTE-*.json")}
    # 事件 → 候选的编号约定：EVT-00N ↔ CTE-00N（本项目的既有映射）
    contradictions = [
        event_id
        for event_id in RECORD_ONLY
        if event_id.replace("EVT-", "CTE-") in existing
    ]
    assert not contradictions, (
        f"这些事件声明「有意不产出候选」，却存在候选文件：{contradictions}"
    )


def test_record_only_events_still_have_retros():
    """只记录的事件**仍然要有复盘** —— 不产出候选不等于不分析。

    这正是它存在的意义：把量化证据留下来，供人做决定。
    """
    retros_dir = DEFAULT_EVOLUTION_DIR / "retros"
    missing = [
        event_id
        for event_id in RECORD_ONLY
        if not (retros_dir / f"{event_id}.md").is_file()
    ]
    assert not missing, f"这些只记录的事件没有复盘：{missing}"


# ── 复盘内容要对应事件 ────────────────────────────────────────────────────────

def test_retros_do_not_borrow_another_events_story():
    """每份复盘的「归因」一节必须谈自己的事，不能串别人的。

    初版把 EVT-001 的征信故事写死在模板里，于是每份复盘都印着
    「关键词表漏了裸词征信」—— 一份描述错误事件的复盘比没有复盘更糟。
    """
    retros_dir = DEFAULT_EVOLUTION_DIR / "retros"
    if not retros_dir.is_dir():
        pytest.skip("没有复盘")

    for path in sorted(retros_dir.glob("EVT-*.md")):
        event_id = path.stem
        if event_id == "EVT-001":
            continue  # 征信确实是它自己的故事
        body = path.read_text(encoding="utf-8")
        assert "关键词表漏了裸词" not in body, (
            f"{path.name} 串了 EVT-001 的归因"
        )


def test_retro_headers_match_their_event_surface():
    """复盘的 surface 标注要与事件注册表一致。"""
    by_id = {spec["event_id"]: spec for spec in EVENT_LOG}
    retros_dir = DEFAULT_EVOLUTION_DIR / "retros"
    if not retros_dir.is_dir():
        pytest.skip("没有复盘")

    for path in sorted(retros_dir.glob("EVT-*.md")):
        spec = by_id.get(path.stem)
        if spec is None:
            continue
        body = path.read_text(encoding="utf-8")
        assert f"surface: `{spec['surface']}`" in body, (
            f"{path.name} 的 surface 标注与注册表不符"
        )
