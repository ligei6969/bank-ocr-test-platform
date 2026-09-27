"""OCR 快照的测试。

这块守的核心是**「系统观测」与「Ground Truth」不能混为一谈**：
快照记录系统实际看到了什么，labels.json 记录应该是什么。
一旦两者混用，评测就变成在测引擎自己 —— 那正是 P5 方案要避免的事。

其余测试围绕几个具体坑：图片被 CI 清理掉、图被重新生成过、
未命中时该报错还是该回退。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_evolution.ocr_snapshot import (
    DEFAULT_SNAPSHOT_PATH,
    OcrObservation,
    OcrSnapshot,
    SnapshotReplay,
    _key,
    file_sha256,
    load,
    save,
    verify_unchanged,
)


def _observation(path: str = "data/processed/bank_card/normal/bank_card_0001.png", **over):
    payload = {
        "image_path": path,
        "image_sha256": "deadbeef",
        "ocr_texts": ["TEST BANK", "6222 0202 0202 0001"],
        "parsed_fields": {"card_number": "6222020202020001", "name": "ZHANG SAN"},
        "quality": {"quality_result": "pass", "quality_reasons": [], "quality_metrics": {}},
        "engine": "paddleocr",
        "engine_version": "paddleocr=3.7.0",
        "recorded_at": "2026-09-27T00:00:00+00:00",
        "doc_type": "bank_card",
    }
    payload.update(over)
    return OcrObservation(**payload)


# ── 路径归一 ──────────────────────────────────────────────────────────────────

def test_keys_are_repo_relative_with_forward_slashes():
    """快照要入库，绝对路径换台机器就失效。"""
    windows = r"J:\job\bank-ocr-test-platform\data\processed\bank_card\normal\bank_card_0001.png"

    assert _key(windows) == "data/processed/bank_card/normal/bank_card_0001.png"


def test_relative_and_absolute_paths_resolve_to_the_same_key():
    absolute = r"J:\job\bank-ocr-test-platform\data\processed\bank_card\normal\bank_card_0001.png"
    relative = "data/processed/bank_card/normal/bank_card_0001.png"

    assert _key(absolute) == _key(relative)


# ── 系统观测 vs Ground Truth ──────────────────────────────────────────────────

def test_snapshot_keeps_full_card_number_unmasked():
    """快照不脱敏 —— 脱敏策略管日志与 LLM 载荷，不管仓内评测数据。

    存成 ``******`` 的话，``rule_check.is_valid_card_number`` 拿不到卡号，
    每张卡都会被误判成 ``invalid_card_number``，评测直接垮。
    """
    obs = _observation()
    card = obs.parsed_fields["card_number"]

    assert "*" not in card
    assert card.isdigit()


def test_observation_records_the_engine_and_its_version():
    """没有引擎版本，快照无法回答「这个行为是哪个版本产生的」。"""
    payload = _observation().to_dict()
    runtime = payload["ocr_runtime"]

    assert runtime["engine"] == "paddleocr"
    assert "paddleocr=" in runtime["engine_version"]
    assert runtime["recorded_at"]
    assert runtime["platform"], "跨平台 diff 需要知道录制平台"


def test_observation_records_the_image_hash():
    """图被重新生成过而快照没更新 —— 靠哈希才认得出。"""
    assert _observation().image_sha256


def test_roundtrip_preserves_observations(tmp_path):
    snapshot = OcrSnapshot(
        observations={_key(_observation().image_path): _observation()},
        recorded_at="2026-09-27T00:00:00+00:00",
        notes=["测试"],
    )
    path = save(snapshot, tmp_path / "snap.json")
    loaded = load(path)

    assert loaded is not None
    obs = loaded.get("data/processed/bank_card/normal/bank_card_0001.png")
    assert obs is not None
    assert obs.parsed_fields["card_number"] == "6222020202020001"
    assert obs.engine_version == "paddleocr=3.7.0"
    assert loaded.notes == ["测试"]


def test_missing_snapshot_returns_none_not_an_error(tmp_path):
    """「还没录过」是合法状态，与「快照损坏」不是一回事。"""
    assert load(tmp_path / "不存在.json") is None


# ── 回放：未命中即失败 ────────────────────────────────────────────────────────

def test_replay_raises_on_a_miss_by_default():
    """未命中意味着评测结果不可信 —— 报错，不静默回退到标注真值。"""
    replay = SnapshotReplay(OcrSnapshot(observations={}))

    with pytest.raises(KeyError, match="未命中 OCR 快照"):
        replay.fields_for("data/processed/bank_card/normal/bank_card_0001.png")


def test_replay_records_misses_when_not_strict():
    replay = SnapshotReplay(OcrSnapshot(observations={}), strict=False)

    assert replay.fields_for("data/x.png") is None
    assert len(replay.misses) == 1
    assert replay.misses[0].image_path == "data/x.png"


def test_replay_serves_quality_so_ci_need_not_read_images():
    """CI 会删掉部分图，质量检测必须能从快照拿 —— 否则那几条样本必失败。"""
    obs = _observation()
    replay = SnapshotReplay(OcrSnapshot(observations={_key(obs.image_path): obs}))

    quality = replay.quality_for(obs.image_path)

    assert quality is not None
    assert quality["quality_result"] == "pass"


def test_replay_returns_copies_not_references():
    """回放返回的字典被下游改掉，不该污染快照本身。"""
    obs = _observation()
    replay = SnapshotReplay(OcrSnapshot(observations={_key(obs.image_path): obs}))

    fields = replay.fields_for(obs.image_path)
    fields["card_number"] = "被改了"

    again = replay.fields_for(obs.image_path)
    assert again["card_number"] == "6222020202020001"


# ── 图变了要发现 ──────────────────────────────────────────────────────────────

def test_a_regenerated_image_is_reported_as_stale(tmp_path):
    image = tmp_path / "data" / "processed" / "a.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"first")

    obs = _observation("data/processed/a.png", image_sha256=file_sha256(image))
    snapshot = OcrSnapshot(observations={_key(obs.image_path): obs})

    assert verify_unchanged(snapshot, base_dir=tmp_path) == []

    image.write_bytes(b"second")  # 图重新生成过，快照没更新

    assert verify_unchanged(snapshot, base_dir=tmp_path) == ["data/processed/a.png"]


def test_a_missing_image_is_not_reported_as_stale(tmp_path):
    """CI 会删图来缩小检出体积 —— 图不在不等于快照失效。

    这正是快照存在的意义：让评测不必读图。
    """
    obs = _observation("data/processed/不存在.png")
    snapshot = OcrSnapshot(observations={_key(obs.image_path): obs})

    assert verify_unchanged(snapshot, base_dir=tmp_path) == []


# ── 与评测层的接线 ────────────────────────────────────────────────────────────

def test_eval_prefers_the_snapshot_over_labels():
    """评测取字段时，快照必须优先于标注真值 —— 否则等于没做这件事。"""
    from ai_service.eval.platform_rules import fields_for
    from ai_service.eval.golden import GoldenSample

    sample = GoldenSample(
        sample_id="s", image_path="data/processed/bank_card/normal/bank_card_0001.png",
        doc_type="bank_card", quality_type="normal", expected_reason_codes=(),
        expected_quality_result="pass", expected_tools=(), expects_escalation=False,
        fields={"card_number": "标注里的值"},
    )
    obs = _observation()
    snapshot = SnapshotReplay(OcrSnapshot(observations={_key(obs.image_path): obs}))

    fields, source = fields_for(sample, snapshot=snapshot)

    assert source == "ocr_snapshot"
    assert fields["card_number"] == "6222020202020001", "不该拿标注真值"


def test_eval_labels_fallback_is_labelled_honestly():
    """没有快照时用标注真值，但**来源要如实标出来**。

    「结论正确率 0.775」在两种输入下的含义完全不同 ——
    不写来源的数字会被后来者当成同一件事比较。
    """
    from ai_service.eval.platform_rules import fields_for
    from ai_service.eval.golden import GoldenSample

    sample = GoldenSample(
        sample_id="s", image_path="data/processed/x.png", doc_type="bank_card",
        quality_type="normal", expected_reason_codes=(), expected_quality_result="pass",
        expected_tools=(), expects_escalation=False,
        fields={"card_number": "标注里的值"},
    )

    fields, source = fields_for(sample, snapshot=None)

    assert source == "labels"
    assert fields["card_number"] == "标注里的值"


def test_quality_falls_back_to_reading_the_image_without_a_snapshot(tmp_path, monkeypatch):
    """没有快照时保持本地开发的便利：现读图片。"""
    from ai_service.eval import platform_rules

    called = {}

    def fake_check(path):
        called["path"] = path
        return {"quality_result": "pass", "quality_reasons": []}

    monkeypatch.setattr("app.quality_check.check_image_quality", fake_check)
    image = tmp_path / "a.png"
    image.write_bytes(b"x")

    result = platform_rules.compute_quality(str(image), snapshot=None)

    assert result["quality_result"] == "pass"
    assert called["path"] == str(image)


def test_snapshot_survives_the_ci_image_cleanup(tmp_path, monkeypatch):
    """**CI 的门禁暗坑**：图片被清理掉之后，评测必须仍然拿得到质量数据。

    ``.github/workflows/tests.yml`` 会删掉每类第 4 张及之后的图来缩小检出
    体积，而 golden 的 ``_balanced_take`` 每桶取到 4 张 —— 于是 CI 里
    那 5 条 ``*-03`` 样本的 ``compute_quality`` 走 ``path.is_file()`` 失败，
    返回 ``quality_check_unavailable``。门禁当时没红只是被容忍度吃掉了。

    实测过：把 ``bank_card_*_0004/0005.png`` 移走后，
    不用快照有 5/40 条拿不到质量数据，用快照是 0/40。
    """
    from ai_service.eval import platform_rules

    def boom(path):
        raise AssertionError(f"不应该去读图：{path}")

    monkeypatch.setattr("app.quality_check.check_image_quality", boom)

    obs = _observation("data/processed/bank_card/blur/bank_card_0004.png")
    snapshot = SnapshotReplay(OcrSnapshot(observations={_key(obs.image_path): obs}))

    # 图不存在（模拟 CI 删过）+ 只给快照 —— 仍然拿得到质量结果
    result = platform_rules.compute_quality(obs.image_path, snapshot=snapshot)

    assert result["quality_result"] == "pass"


def test_without_a_snapshot_a_deleted_image_degrades(tmp_path, monkeypatch):
    """对照：没有快照时，图被删掉就拿不到质量数据 —— 这正是快照要解决的问题。"""
    from ai_service.eval import platform_rules

    missing = tmp_path / "被删掉的图.png"

    result = platform_rules.compute_quality(str(missing), snapshot=None)

    assert result == {}


def test_quality_uses_the_snapshot_when_present(tmp_path):
    """有快照就不该去读图 —— CI 里那张图可能已经被删了。"""
    from ai_service.eval import platform_rules

    obs = _observation("data/processed/被删掉的图.png")
    snapshot = SnapshotReplay(OcrSnapshot(observations={_key(obs.image_path): obs}))

    result = platform_rules.compute_quality(obs.image_path, snapshot=snapshot)

    assert result["quality_result"] == "pass"


# ── 快照文件的形状 ────────────────────────────────────────────────────────────

def test_default_snapshot_path_sits_with_the_labels():
    """快照是评测输入的一部分，不是运行时产物 —— 不该放 reports/。"""
    assert DEFAULT_SNAPSHOT_PATH.parent.name == "annotations"
    assert DEFAULT_SNAPSHOT_PATH.suffix == ".json"


# ── 断点续录 ──────────────────────────────────────────────────────────────────

def test_recording_writes_checkpoints_as_it_goes(tmp_path, monkeypatch):
    """边录边写 —— 全量 2100 张要跑近半小时，崩了不能全丢。

    实测踩到过：PaddleOCR 在长循环里抛 ``CUDA error(700)``，进程整个挂掉。
    没有断点时那半小时的成果全部丢失，只因为最后一张图失败。
    """
    from test_evolution import ocr_snapshot as mod

    calls = []

    def fake_observe(path, *, doc_type, ocr_mode, recorded_at):
        calls.append(str(path))
        return _observation(f"data/processed/{Path(path).name}")

    monkeypatch.setattr(mod, "observe_image", fake_observe)

    images = []
    for index in range(5):
        path = tmp_path / f"img{index}.png"
        path.write_bytes(b"x")
        images.append(path)

    checkpoint = tmp_path / "snap.json"
    mod.record(
        images,
        doc_type_of=lambda p: "bank_card",
        checkpoint_path=checkpoint,
        progress_every=2,
    )

    assert checkpoint.is_file()
    assert len(calls) == 5


def test_resume_skips_images_already_recorded(tmp_path, monkeypatch):
    """续录要跳过已完成的 —— 那正是断点存在的意义。"""
    from test_evolution import ocr_snapshot as mod

    images = []
    for index in range(4):
        path = tmp_path / f"img{index}.png"
        path.write_bytes(b"x")
        images.append(path)

    checkpoint = tmp_path / "snap.json"
    seen: list = []

    def fake_observe(path, *, doc_type, ocr_mode, recorded_at):
        seen.append(Path(path).name)
        return _observation(f"data/processed/{Path(path).name}")

    monkeypatch.setattr(mod, "observe_image", fake_observe)

    # 第一次只录前两张（模拟中断）
    mod.record(images[:2], doc_type_of=lambda p: "bank_card", checkpoint_path=checkpoint)
    first_round = list(seen)

    # 第二次给全量：只该录后两张
    seen.clear()
    snapshot = mod.record(
        images, doc_type_of=lambda p: "bank_card", checkpoint_path=checkpoint
    )

    assert first_round == ["img0.png", "img1.png"]
    assert sorted(seen) == ["img2.png", "img3.png"], "已录的不该重复录"
    assert len(snapshot.observations) == 4, "合并后应含全部四张"


def test_restart_ignores_the_checkpoint(tmp_path, monkeypatch):
    """``resume=False`` 时从头录 —— 引擎升级后需要整批重录。"""
    from test_evolution import ocr_snapshot as mod

    image = tmp_path / "img0.png"
    image.write_bytes(b"x")
    checkpoint = tmp_path / "snap.json"

    seen: list = []

    def fake_observe(path, *, doc_type, ocr_mode, recorded_at):
        seen.append(Path(path).name)
        return _observation(f"data/processed/{Path(path).name}")

    monkeypatch.setattr(mod, "observe_image", fake_observe)

    mod.record([image], doc_type_of=lambda p: "bank_card", checkpoint_path=checkpoint)
    seen.clear()
    mod.record(
        [image],
        doc_type_of=lambda p: "bank_card",
        checkpoint_path=checkpoint,
        resume=False,
    )

    assert seen == ["img0.png"], "resume=False 应重录"


def test_no_checkpoint_leaves_no_file_until_the_end(tmp_path, monkeypatch):
    """不给断点时行为与以前一致 —— 小批量录制不需要中间产物。"""
    from test_evolution import ocr_snapshot as mod

    image = tmp_path / "img0.png"
    image.write_bytes(b"x")

    monkeypatch.setattr(
        mod,
        "observe_image",
        lambda path, *, doc_type, ocr_mode, recorded_at: _observation(
            f"data/processed/{Path(path).name}"
        ),
    )

    mod.record([image], doc_type_of=lambda p: "bank_card")

    assert not list(tmp_path.glob("*.json"))


def test_recorder_targets_cover_every_golden_image():
    """录制目标必须**完整覆盖** golden 集用到的每一张图。

    这条测试守着一个实测踩到的坑：录制脚本原本自己按桶排序取前 N 张，
    取到 ``back/blur/0001..0004``；而 golden 的 ``_balanced_take`` 是
    按面轮转的，它要 ``back/blur/0001`` 和 ``front/blur/0001``。
    两套选取策略 → CI 回放时 40 条里 10 条未命中。

    现在录制直接复用 ``build_golden_set``。这条测试保证它不会哪天又
    被改成「另一套看起来更合理的策略」。
    """
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.record_ocr_snapshot import load_targets

    from ai_service.eval.golden import load_golden_set

    golden_keys = {_key(s.image_path) for s in load_golden_set().samples}
    recorded_keys = {_key(str(path)) for path, _ in load_targets()}

    missing = golden_keys - recorded_keys
    assert not missing, f"这些 golden 图不会被录制，CI 回放必然未命中：{sorted(missing)}"


def test_snapshot_declares_its_schema_version():
    """格式变了要能识别出老快照，而不是误读。"""
    assert OcrSnapshot().schema_version >= 1
    assert OcrSnapshot().to_dict()["schema_version"] >= 1
