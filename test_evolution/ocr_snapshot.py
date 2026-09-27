"""OCR 快照：把「真实 PaddleOCR 曾经跑出什么」录下来，让 CI 离线回放。

为什么需要这个
--------------
评测集里的 ``fields`` 目前来自 ``labels.json``（标注真值），不是 OCR 实际输出，
于是「字段全部解析成功」恒成立 —— 这个信号没有区分度，也正是
OCR 与双判两个 surface 被卡住的原因（见 :mod:`test_evolution.readiness`）。

解决办法不是在 CI 里现场跑 PaddleOCR：那会让回归门禁依赖模型版本、
每次重新下载权重、并把 Linux 与开发者本地的浮点差异引进来，
边界样本（blur/glare 阈值附近）的识别结果会随机翻转，门禁就会随机打红。

用和 LLM 完全相同的哲学：

    真实 PaddleOCR → 录制 → 快照文件 → CI 回放

**快照记录的是「系统观测」（System Observation），不是 Ground Truth。**
两者的区别是这份设计的核心：

* Ground Truth（``labels.json``）—— 这张图**应该**是什么，人标的，永久有效
* System Observation（快照）—— 系统**实际**看到了什么，引擎跑的，附引擎版本

前者用于判定对错，后者用于追踪行为。混为一谈会导致「OCR 输出被当成答案」，
那样评测就变成在测引擎自己。

快照存什么
----------
不只存最终字段。出了错要能分清是 **OCR 认错了** 还是 **parser 解析错了**，
所以三个都存：原始文本行、解析后的字段、图像质量指标。

质量指标必须存 —— CI 里图片可能不在（``.github/workflows/tests.yml`` 会删掉
每类第 4 张及之后的图来缩小检出体积），而 ``platform_rules.compute_quality``
是**现读图片**的。快照带上质量指标，CI 就完全不用读图。

敏感字段不脱敏
--------------
快照里的卡号是**完整**的，与 ``labels.json`` 一致。脱敏策略管的是
日志与外部 LLM 载荷（``logging_utils`` / ``policy.sanitize_question``），
不管仓内评测数据 —— 存脱敏值会让 ``rule_check.is_valid_card_number``
拿不到卡号，每张卡都被误判成 ``invalid_card_number``。
这些图本来就是合成数据（AGENTS.md 明令不得提交真实证件）。
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]

#: 快照文件。放在 ``data/annotations/`` 与 labels.json 并列 ——
#: 它是评测输入的一部分，不是运行时产物。
DEFAULT_SNAPSHOT_PATH = ROOT_DIR / "data" / "annotations" / "ocr_outputs.json"

#: 快照 schema 版本。格式变了就升版本，让老快照能被识别出来而不是被误读。
SNAPSHOT_SCHEMA_VERSION = 1


@dataclass
class OcrObservation:
    """一张图的系统观测记录。"""

    image_path: str
    image_sha256: str
    ocr_texts: List[str]
    parsed_fields: Dict[str, Any]
    quality: Dict[str, Any]
    engine: str
    engine_version: str
    recorded_at: str
    doc_type: str = ""
    parse_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "image_path": self.image_path,
            "image_sha256": self.image_sha256,
            "doc_type": self.doc_type,
            "ocr_texts": list(self.ocr_texts),
            "parsed_fields": dict(self.parsed_fields),
            "quality": dict(self.quality),
            "parse_error": self.parse_error,
            "ocr_runtime": {
                "engine": self.engine,
                "engine_version": self.engine_version,
                "python": platform.python_version(),
                "platform": sys.platform,
                "recorded_at": self.recorded_at,
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OcrObservation":
        runtime = payload.get("ocr_runtime") or {}
        return cls(
            image_path=str(payload.get("image_path") or ""),
            image_sha256=str(payload.get("image_sha256") or ""),
            ocr_texts=list(payload.get("ocr_texts") or []),
            parsed_fields=dict(payload.get("parsed_fields") or {}),
            quality=dict(payload.get("quality") or {}),
            engine=str(runtime.get("engine") or ""),
            engine_version=str(runtime.get("engine_version") or ""),
            recorded_at=str(runtime.get("recorded_at") or ""),
            doc_type=str(payload.get("doc_type") or ""),
            parse_error=str(payload.get("parse_error") or ""),
        )


@dataclass
class OcrSnapshot:
    """一份完整的快照，含元信息。"""

    observations: Dict[str, OcrObservation] = field(default_factory=dict)
    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    recorded_at: str = ""
    notes: List[str] = field(default_factory=list)

    def get(self, image_path: str) -> Optional[OcrObservation]:
        return self.observations.get(_key(image_path))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "recorded_at": self.recorded_at,
            "notes": list(self.notes),
            "observations": {
                key: obs.to_dict() for key, obs in sorted(self.observations.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OcrSnapshot":
        raw = payload.get("observations") or {}
        return cls(
            observations={
                key: OcrObservation.from_dict(value) for key, value in raw.items()
            },
            schema_version=int(payload.get("schema_version") or 0),
            recorded_at=str(payload.get("recorded_at") or ""),
            notes=list(payload.get("notes") or []),
        )


def _key(image_path: str) -> str:
    """快照的查表键：**正斜杠的仓库相对路径**。

    为什么不用绝对路径：快照要入库，绝对路径换台机器就失效。
    为什么不只按 sha256：按哈希查表在 diff 里读不出是哪张图。
    """
    normalized = str(image_path).replace("\\", "/")
    marker = "data/"
    index = normalized.find(marker)
    if index > 0:
        normalized = normalized[index:]
    return normalized.lstrip("./")


def file_sha256(path: Path) -> str:
    """整文件哈希。图片换了一张但路径没变时，靠它认出来。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def engine_version() -> str:
    """取 PaddleOCR / Paddle 版本。缺失时返回 ``unknown`` 而不是抛错 ——
    版本读不到不该让整次录制失败，它只是元信息。"""
    parts: List[str] = []
    for module_name in ("paddleocr", "paddle"):
        try:
            module = __import__(module_name)
            parts.append(f"{module_name}={getattr(module, '__version__', 'unknown')}")
        except ImportError:
            parts.append(f"{module_name}=absent")
    return ";".join(parts)


# ── 录制 ──────────────────────────────────────────────────────────────────────

def observe_image(
    image_path: Path,
    *,
    doc_type: str,
    ocr_mode: str = "paddle",
    recorded_at: str = "",
) -> OcrObservation:
    """跑一次真实 OCR + 解析 + 质量检测，录成一条观测。

    这条路径**必须**与 CI 回放走的是同一套解析代码 —— 否则录制与回放
    测的不是一回事。字段解析直接调 ``app`` 里的真实实现，不另写一份。
    """
    from app.field_parser import parse_bank_card_fields
    from app.id_card_parser import (
        detect_id_card_side,
        parse_id_card_back_fields,
        parse_id_card_front_fields,
    )
    from app.ocr_service import recognize_text
    from app.quality_check import check_image_quality

    texts = recognize_text(str(image_path), mode=ocr_mode)  # type: ignore[arg-type]
    joined = "\n".join(texts)
    parse_error = ""
    parsed: Dict[str, Any] = {}

    try:
        if doc_type == "bank_card":
            parsed = parse_bank_card_fields(joined)
        elif doc_type == "id_card":
            side = detect_id_card_side(joined)
            if side == "back":
                parsed = parse_id_card_back_fields(joined)
            else:
                parsed = parse_id_card_front_fields(joined)
            parsed["_side"] = side
        else:
            parse_error = f"unsupported doc_type: {doc_type}"
    except Exception as exc:  # noqa: BLE001 - 录制要尽量多录，单张失败不该中断整轮
        parse_error = f"{type(exc).__name__}: {exc}"

    try:
        quality = check_image_quality(str(image_path))
    except (ValueError, OSError) as exc:
        quality = {}
        parse_error = parse_error or f"quality_check failed: {exc}"

    return OcrObservation(
        image_path=_key(str(image_path)),
        image_sha256=file_sha256(image_path),
        ocr_texts=list(texts),
        parsed_fields=parsed,
        quality=quality,
        engine="paddleocr" if ocr_mode == "paddle" else ocr_mode,
        engine_version=engine_version(),
        recorded_at=recorded_at,
        doc_type=doc_type,
        parse_error=parse_error,
    )


def record(
    image_paths: Iterable[Path],
    *,
    doc_type_of,
    ocr_mode: str = "paddle",
    recorded_at: str = "",
    notes: Sequence[str] = (),
) -> OcrSnapshot:
    """录制一批图。

    ``doc_type_of`` 是 ``(path) -> doc_type`` 的函数 —— 一张图属于银行卡
    还是身份证，只有调用方知道（快照不应该去猜目录结构）。
    """
    observations: Dict[str, OcrObservation] = {}
    for path in image_paths:
        path = Path(path)
        if not path.is_file():
            continue
        obs = observe_image(
            path,
            doc_type=doc_type_of(path),
            ocr_mode=ocr_mode,
            recorded_at=recorded_at,
        )
        observations[_key(str(path))] = obs

    return OcrSnapshot(
        observations=observations,
        recorded_at=recorded_at,
        notes=list(notes),
    )


# ── 读写 ──────────────────────────────────────────────────────────────────────

def save(snapshot: OcrSnapshot, path: Path = DEFAULT_SNAPSHOT_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load(path: Optional[Path] = None) -> Optional[OcrSnapshot]:
    """读快照。文件不存在返回 ``None`` —— 调用方据此决定回退还是报错。

    不在这里抛错，是因为「没有快照」是一个**合法状态**（还没录过），
    与「快照损坏」不是一回事。
    """
    resolved = Path(path) if path is not None else DEFAULT_SNAPSHOT_PATH
    if not resolved.is_file():
        return None
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    return OcrSnapshot.from_dict(payload)


# ── 回放 ──────────────────────────────────────────────────────────────────────

@dataclass
class SnapshotMiss:
    """快照未命中。CI 里这是失败，不是降级。"""

    image_path: str
    reason: str


class SnapshotReplay:
    """把快照包装成评测可用的「系统观测来源」。

    评测层拿到它之后就不必读图 —— 这既让 CI 摆脱 PaddleOCR，
    也顺手解决了「图片被清理步骤删掉」的问题。
    """

    def __init__(self, snapshot: OcrSnapshot, *, strict: bool = True) -> None:
        self.snapshot = snapshot
        self.strict = strict
        self.misses: List[SnapshotMiss] = []

    def fields_for(self, image_path: str) -> Optional[Dict[str, Any]]:
        obs = self.snapshot.get(image_path)
        if obs is None:
            self._miss(image_path, "该图不在快照里")
            return None
        return dict(obs.parsed_fields)

    def quality_for(self, image_path: str) -> Optional[Dict[str, Any]]:
        obs = self.snapshot.get(image_path)
        if obs is None:
            self._miss(image_path, "该图不在快照里")
            return None
        return dict(obs.quality)

    def _miss(self, image_path: str, reason: str) -> None:
        self.misses.append(SnapshotMiss(_key(image_path), reason))
        if self.strict:
            raise KeyError(
                f"{_key(image_path)} 未命中 OCR 快照（{reason}）。"
                "快照是评测输入的一部分 —— 未命中意味着评测结果不可信，"
                "所以这里报错而不是静默回退到标注真值。"
            )


def verify_unchanged(snapshot: OcrSnapshot, *, base_dir: Path = ROOT_DIR) -> List[str]:
    """检查快照里记录的图**内容**有没有变过。

    快照按内容哈希认图：图被重新生成过（换了字体、改了清晰度）而快照没更新，
    回放出来的就是一张已经不存在于仓库的图的观测。这时评测结果没意义。
    返回有问题的清单（空清单 = 全部一致）。
    """
    stale: List[str] = []
    for key, obs in sorted(snapshot.observations.items()):
        path = Path(base_dir) / key
        if not path.is_file():
            # 图不在不等于快照失效：CI 会删掉部分图来缩小检出体积，
            # 而快照存在的意义正是让评测不必读图。这里只提示，不算 stale。
            continue
        if file_sha256(path) != obs.image_sha256:
            stale.append(key)
    return stale


__all__ = (
    "DEFAULT_SNAPSHOT_PATH",
    "SNAPSHOT_SCHEMA_VERSION",
    "OcrObservation",
    "OcrSnapshot",
    "SnapshotMiss",
    "SnapshotReplay",
    "engine_version",
    "file_sha256",
    "load",
    "observe_image",
    "record",
    "save",
    "verify_unchanged",
)
