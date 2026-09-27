"""把 OCR 快照变成系统化的字段错误率基线。

为什么这个报告是 CTE-5 的真正产出
---------------------------------
CTE-2 只录了 golden 用到的 50 张图 —— 够跑评测，但**不足以回答**
「降质样本的字段错误率是多少」这类问题：每桶 5 张，一个百分点就是 20%。

而这个数字是**产品决策的输入**：反光严重度阈值该不该启用、启在哪，
需要一个依据说明「反光到什么程度时字段就读不出来了」。当前那个阈值是
刻意停用的（标定间隙只有 9%），解除停用需要的正是这个分布。

所以本模块把快照按「退化类型 × 字段」切开，给出可比的错误率表格。

两种错误率，必须分开看
----------------------
对每个字段，分别统计：

* **解析失败率** —— 解析结果为空。可能是 OCR 没认出来，也可能是解析器没取到；
* **数值不符率** —— 解析出来了但与标注真值不一致（认错了）。

第三类「文本里没有证据」用 :mod:`app.ocr_evidence` 判定 ——
它把第一类再拆成「OCR 限制」与「解析器责任」。**三者混在一起看会误导改进方向**，
这正是 EVT-002/EVT-003 记下的教训。

为什么用标注真值比对
--------------------
``labels.json`` 的 ``fields`` 是**人工标注的真值**，而快照的
``parsed_fields`` 是系统实际输出。这里比的是「系统对不对」，
所以拿真值当基准是正确的 —— 与评测层「用真值当输入」是两回事。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from test_evolution.ocr_snapshot import OcrSnapshot, load

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LABELS_PATH = ROOT_DIR / "data" / "annotations" / "labels.json"

#: 各 doc_type 需要统计的字段。与 ``app`` 里各解析器的输出对齐。
FIELDS_BY_DOC_TYPE: Dict[str, Tuple[str, ...]] = {
    "bank_card": ("card_number", "name", "valid_date"),
    "id_card": ("name", "gender", "nation", "birth", "address", "id_number", "issue_authority", "valid_period"),
}

#: ``labels.json`` 的 doc_type → 快照的 doc_type。
DOC_TYPE_NORMALIZATION = {
    "bank_card": "bank_card",
    "id_card_front": "id_card",
    "id_card_back": "id_card",
}


def _normalize(value: Any) -> str:
    """比对用的归一：去空白、去常见分隔符、转大写。

    卡号带空格（``6222 0202 0202 0001``）、有效期格式在 mock 与真实之间有差异
    （``12/30`` vs ``12/30``）—— 不归一会把格式差异算成识别错误。
    """
    return re.sub(r"[\s\-]", "", str(value or "")).upper()


@dataclass
class FieldStats:
    """一个字段在一个桶里的统计。"""

    total: int = 0
    parsed: int = 0
    """解析出了非空值（不管对不对）。"""
    correct: int = 0
    """与标注真值一致。"""
    wrong_value: int = 0
    """解析出了值但不对。"""
    missing_with_evidence: int = 0
    """解析为空，但文本里有证据 —— **解析器责任**。"""
    missing_without_evidence: int = 0
    """解析为空，且文本里没证据 —— **OCR 限制**。"""

    @property
    def missing(self) -> int:
        return self.missing_with_evidence + self.missing_without_evidence

    @property
    def accuracy(self) -> Optional[float]:
        return self.correct / self.total if self.total else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "parsed": self.parsed,
            "correct": self.correct,
            "wrong_value": self.wrong_value,
            "missing_with_evidence": self.missing_with_evidence,
            "missing_without_evidence": self.missing_without_evidence,
            "accuracy": self.accuracy,
        }


@dataclass
class BucketReport:
    """一个 (doc_type, quality_type) 桶的报告。"""

    doc_type: str
    quality_type: str
    fields: Dict[str, FieldStats] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.doc_type}/{self.quality_type}"

    @property
    def total(self) -> int:
        """桶内样本数（所有字段的 total 相同）。"""
        for stats in self.fields.values():
            return stats.total
        return 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "quality_type": self.quality_type,
            "samples": self.total,
            "fields": {name: stats.to_dict() for name, stats in self.fields.items()},
        }


def _truth_record(item: Mapping[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """从 labels.json 的一条里取出 ``(doc_type, quality_type, fields)``。"""
    doc_type = DOC_TYPE_NORMALIZATION.get(str(item.get("doc_type") or ""), "")
    quality_type = str(item.get("quality_type") or "")
    raw_fields = item.get("fields")
    return doc_type, quality_type, dict(raw_fields) if isinstance(raw_fields, dict) else {}


def build_report(
    snapshot: OcrSnapshot,
    *,
    labels_path: Path = DEFAULT_LABELS_PATH,
) -> Dict[str, BucketReport]:
    """按 (doc_type, quality_type) 分桶统计字段错误率。

    只统计**快照里有观测**的图 —— 没录的样本不参与，也不当成失败。
    """
    from app.ocr_evidence import has_evidence

    labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    buckets: Dict[str, BucketReport] = {}

    for item in labels:
        doc_type, quality_type, truth = _truth_record(item)
        if not doc_type:
            continue
        image_path = str(item.get("image_path") or "")
        observed = snapshot.get(image_path)
        if observed is None:
            continue  # 没录进快照，不参与统计

        key = f"{doc_type}/{quality_type}"
        report = buckets.setdefault(key, BucketReport(doc_type, quality_type))
        texts = "\n".join(observed.ocr_texts)
        parsed = observed.parsed_fields

        for name in FIELDS_BY_DOC_TYPE.get(doc_type, ()):
            if name not in truth:
                continue
            stats = report.fields.setdefault(name, FieldStats())
            stats.total += 1

            got = parsed.get(name)
            expected = truth.get(name)

            if got:
                stats.parsed += 1
                if _normalize(got) == _normalize(expected):
                    stats.correct += 1
                else:
                    stats.wrong_value += 1
                continue

            # 解析为空：区分是 OCR 没认出来还是解析器没取到
            if has_evidence(name, texts):
                stats.missing_with_evidence += 1
            else:
                stats.missing_without_evidence += 1

    return buckets


# ── 渲染 ──────────────────────────────────────────────────────────────────────

def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:5.1f}%"


def render_markdown(buckets: Mapping[str, BucketReport], *, snapshot: OcrSnapshot) -> str:
    """渲染成人可读的 markdown 报告。"""
    lines: List[str] = []
    lines.append("# OCR 字段错误率基线")
    lines.append("")
    lines.append(f"> 快照录制于 {snapshot.recorded_at}，共 {len(snapshot.observations)} 条观测。")
    lines.append("> 由 `test_evolution/ocr_report.py` 生成 —— 请勿手工编辑。")
    lines.append("")
    lines.append("**三列失败原因必须分开看**：`解析缺失` 里既有 OCR 没认出来的，")
    lines.append("也有解析器没取到的；`值不符` 是认出来了但认错了。")
    lines.append("后两列把「解析缺失」拆开 —— 它们指向完全不同的改进方向。")
    lines.append("")

    for key in sorted(buckets):
        report = buckets[key]
        lines.append(f"## {key}（{report.total} 张）")
        lines.append("")
        lines.append("| 字段 | 正确率 | 值不符 | 解析缺失 | ↳ 解析器责任 | ↳ OCR 限制 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for name in FIELDS_BY_DOC_TYPE.get(report.doc_type, ()):
            stats = report.fields.get(name)
            if stats is None or not stats.total:
                continue
            n = stats.total
            lines.append(
                f"| `{name}` | {_pct(stats.accuracy)} | "
                f"{stats.wrong_value / n * 100:.1f}% | "
                f"{stats.missing / n * 100:.1f}% | "
                f"{stats.missing_with_evidence / n * 100:.1f}% | "
                f"{stats.missing_without_evidence / n * 100:.1f}% |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def render_csv(buckets: Mapping[str, BucketReport]) -> str:
    """渲染成 CSV，便于后续做图或对比。"""
    rows = [
        "doc_type,quality_type,field,samples,correct,accuracy,wrong_value,"
        "missing,missing_with_evidence,missing_without_evidence"
    ]
    for key in sorted(buckets):
        report = buckets[key]
        for name in FIELDS_BY_DOC_TYPE.get(report.doc_type, ()):
            stats = report.fields.get(name)
            if stats is None or not stats.total:
                continue
            accuracy = "" if stats.accuracy is None else f"{stats.accuracy:.4f}"
            rows.append(
                f"{report.doc_type},{report.quality_type},{name},{stats.total},"
                f"{stats.correct},{accuracy},{stats.wrong_value},{stats.missing},"
                f"{stats.missing_with_evidence},{stats.missing_without_evidence}"
            )
    return "\n".join(rows) + "\n"


def blocked_surface_evidence(buckets: Mapping[str, BucketReport]) -> Dict[str, Any]:
    """给 `readiness` 用的证据摘要：降质样本的错误率到底有多高。

    这是「反光严重度阈值该不该启用」这类决策的输入 —— 不是结论。
    本函数只汇总事实，**不做阈值建议**：那是产品口径。
    """
    summary: Dict[str, Any] = {}
    for key in sorted(buckets):
        report = buckets[key]
        total = report.total
        if not total:
            continue
        fields = report.fields
        accuracy = [
            stats.accuracy for stats in fields.values() if stats.accuracy is not None
        ]
        missing = sum(stats.missing for stats in fields.values())
        slots = sum(stats.total for stats in fields.values())
        summary[key] = {
            "samples": total,
            "mean_field_accuracy": sum(accuracy) / len(accuracy) if accuracy else None,
            "missing_rate": missing / slots if slots else None,
        }
    return summary


__all__ = (
    "DEFAULT_LABELS_PATH",
    "FIELDS_BY_DOC_TYPE",
    "BucketReport",
    "FieldStats",
    "blocked_surface_evidence",
    "build_report",
    "render_csv",
    "render_markdown",
)
