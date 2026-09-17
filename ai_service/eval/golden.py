"""评测层：golden 数据集、四层指标、judge 校准、回归门禁。

前置检查结论（任务书要求先确认，再决定怎么补标注）
==================================================

先回答那个关键问题：``data/annotations/labels.json`` 标的是**字段**还是**审核结论**？

答案：**都不是审核结论。** 该文件共 2134 条样本，每条包含：

* ``image_path`` / ``doc_type`` / ``side`` —— 样本来源
* ``quality_type`` —— **注入的质量退化类型**（normal / blur / dark / bright / glare / occlusion / rotate）
* ``fields`` —— 该样本的**字段真值**

**没有** ``review_result``（pass/review/reject），**没有**原因码标签。
这直接决定了决策层指标能不能算：

能算的部分
    期望的**质量原因码**可以由 ``quality_type`` 按已文档化的映射推出
    （见 :data:`QUALITY_TYPE_TO_REASONS`）。所以「原因码集合匹配率」是可信的。

不能算的部分
    期望的**审核结论**推不出来 —— 结论取决于质量判定与字段解析的联合结果，
    而字段层没有结论标注。所以任务层的「结论正确率」只能退化成
    「原因码集合匹配率」这个**代理指标**，报告里会显式标为 ``proxy``。

    要算真正的结论层指标，必须先补一份人工结论标注。这是 P2 的输入，
    不是本阶段能靠代码补上的东西 —— 与其编一份假标注，不如把缺口写在报告里。

``occlusion`` / ``rotate`` 为什么不进 golden 集
----------------------------------------------
这两种退化会丢字段，但平台的 ``quality_check`` 并不检测它们（没有对应阈值），
影响落在字段层 —— 而字段层没有结论标注。所以这两类样本**不进 golden 集**：
宁缺毋滥，而不是给它们安一个推不出来的期望值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 平台标注里的退化类型 → 期望的质量原因码。
#: 这张表是 golden 集的全部依据，改它等于改评测口径，所以要显式写在这里。
QUALITY_TYPE_TO_REASONS: Dict[str, Tuple[str, ...]] = {
    "normal": (),
    "blur": ("image_blur",),
    "dark": ("image_dark",),
    "bright": ("image_bright",),
    "glare": ("glare_detected",),
    # occlusion / rotate 有意缺席：平台不判这两类，影响在字段层，
    # 而字段层没有结论标注。见模块 docstring。
}

UNMAPPED_QUALITY_TYPES = ("occlusion", "rotate")

#: 标注文件里的 doc_type → AI 服务认识的 doc_type。
#: ``app/main.py`` 只有 ``/bank-card/review`` 与 ``/id-card/review`` 两条链路，
#: 所以 id_card_front / id_card_back 必须归一到 ``id_card``；
#: ``application_form``（申请表）不在审核链路上，直接排除 —— 喂给服务一个它
#: 从来没见过的 doc_type，检索的 doc_type 过滤会全部落空，
#: 指标会莫名其妙地低，还找不出原因。
DOC_TYPE_NORMALIZATION: Dict[str, str] = {
    "bank_card": "bank_card",
    "id_card": "id_card",
    "id_card_front": "id_card",
    "id_card_back": "id_card",
}

DEFAULT_LABELS_PATH = Path("data/annotations/labels.json")
DEFAULT_TARGET_SIZE = 40
MAX_GOLDEN_SIZE = 50


def normalize_doc_type(raw: str) -> Optional[str]:
    """把标注里的 doc_type 归一成服务认识的取值；不认识返回 ``None``。"""
    return DOC_TYPE_NORMALIZATION.get(str(raw or ""))


@dataclass(frozen=True)
class GoldenSample:
    """一条可评测的样本。

    ``verdict_source`` 天生是 ``derived`` —— 提醒读报告的人：
    任务层指标算的是「原因码对不对」，不是「审核结论对不对」。

    当 ``data/annotations/review_verdicts.json`` 存在且覆盖足够时，
    :func:`load_golden_set` 会把人工结论填进 ``expected_verdict``，
    并把 ``verdict_source`` 改成 ``human`` —— 那时决策层指标才真正可用。
    """

    sample_id: str
    image_path: str
    doc_type: str
    quality_type: str
    expected_reason_codes: Tuple[str, ...]
    expected_quality_result: str
    expected_tools: Tuple[str, ...]
    expects_escalation: bool
    #: 人工标注的期望结论（pass / review / reject）。没有标注时为 None。
    expected_verdict: Optional[str] = None
    verdict_source: str = "derived"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "image_path": self.image_path,
            "doc_type": self.doc_type,
            "quality_type": self.quality_type,
            "expected_reason_codes": list(self.expected_reason_codes),
            "expected_quality_result": self.expected_quality_result,
            "expected_tools": list(self.expected_tools),
            "expects_escalation": self.expects_escalation,
            "expected_verdict": self.expected_verdict,
            "verdict_source": self.verdict_source,
        }

    def to_review_context(self) -> Dict[str, Any]:
        """转成 AI 服务接的审核上下文。

        ``review_result`` 是**推定**的：影像正常且字段有真值 => 判定 pass。
        这一点在 :attr:`verdict_source` 上标记，不要在报告里当成人工结论用。
        """
        normal = not self.expected_reason_codes
        return {
            "request_id": self.sample_id,
            "doc_type": self.doc_type,
            "review_result": "pass" if normal else "review",
            "quality_result": self.expected_quality_result,
            "quality_reasons": list(self.expected_reason_codes),
            "review_reasons": list(self.expected_reason_codes),
            "fields": {},
            "question": "这条记录为什么是这个结论？应该怎么处置？",
        }


def expected_tools_for(
    reason_codes: Sequence[str],
    quality_result: Optional[str],
) -> Tuple[str, ...]:
    """推定该样本的期望工具序列。

    规则与 :meth:`ai_service.agent.ReviewAgent` 的确定性路径一致 ——
    评测的正是那条路径的执行顺序，所以两边必须同源。
    """
    tools: List[str] = ["get_review_record"]
    if reason_codes:
        tools.append("search_knowledge")
    if quality_result:
        tools.append("recompute_quality")
    return tuple(tools)


def build_golden_set(
    labels_path: Path = DEFAULT_LABELS_PATH,
    *,
    target_size: int = DEFAULT_TARGET_SIZE,
    per_bucket: int = 6,
) -> List[GoldenSample]:
    """从平台标注里抽出边界样本，构成 golden 集。

    选取策略：按 ``(doc_type, quality_type)`` 分桶，然后**轮转取**（round-robin）
    每桶各一条，直到凑够 ``target_size``。

    为什么不是「按桶名排序后逐桶取满」—— 那样会按字母序截断：
    排在后面的 ``id_card_front`` / ``id_card_back`` 永远轮不到，
    而它们占了标注文件的大头。样本失衡时的分类指标会骗人，
    所以宁可在每桶里少取几条，也要先把所有桶都覆盖到。

    完全确定性：同一份 labels.json 永远得到同一份 golden 集。
    """
    raw = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for item in raw:
        quality_type = str(item.get("quality_type") or "")
        if quality_type not in QUALITY_TYPE_TO_REASONS:
            continue
        doc_type = normalize_doc_type(str(item.get("doc_type") or ""))
        if doc_type is None:
            continue
        buckets.setdefault((doc_type, quality_type), []).append(item)

    ordered_buckets = [
        (key, sorted(items, key=lambda x: str(x.get("image_path")))[:per_bucket])
        for key, items in sorted(buckets.items())
    ]

    limit = min(target_size, MAX_GOLDEN_SIZE)
    samples: List[GoldenSample] = []
    for round_index in range(per_bucket):
        for (doc_type, quality_type), items in ordered_buckets:
            if round_index >= len(items):
                continue
            reasons = QUALITY_TYPE_TO_REASONS[quality_type]
            quality_result = "pass" if not reasons else "review"
            samples.append(
                GoldenSample(
                    sample_id=f"golden-{doc_type}-{quality_type}-{round_index:02d}",
                    image_path=str(items[round_index].get("image_path") or ""),
                    doc_type=doc_type,
                    quality_type=quality_type,
                    expected_reason_codes=tuple(reasons),
                    expected_quality_result=quality_result,
                    expected_tools=expected_tools_for(reasons, quality_result),
                    expects_escalation=not reasons and quality_result != "pass",
                )
            )
            if len(samples) >= limit:
                return samples
    return samples


def sample_balance(samples: Sequence[GoldenSample]) -> Dict[str, int]:
    """分布统计，写进报告 —— 样本失衡时的指标会误导人。"""
    balance: Dict[str, int] = {}
    for sample in samples:
        key = f"{sample.doc_type}/{sample.quality_type}"
        balance[key] = balance.get(key, 0) + 1
    return balance


@dataclass
class GoldenSet:
    """golden 集 + 它的元信息。"""

    samples: List[GoldenSample] = field(default_factory=list)
    labels_path: str = str(DEFAULT_LABELS_PATH)
    verdict_layer_available: bool = False
    notes: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {
            "size": len(self.samples),
            "labels_path": self.labels_path,
            "balance": sample_balance(self.samples),
            "verdict_layer_available": self.verdict_layer_available,
            "notes": list(self.notes),
        }


def load_golden_set(
    labels_path: Path = DEFAULT_LABELS_PATH,
    *,
    target_size: int = DEFAULT_TARGET_SIZE,
    verdicts_path: Optional[Path] = None,
) -> GoldenSet:
    """加载 golden 集，并附上「哪一层指标不可用」的说明。

    决策层取决于 ``data/annotations/review_verdicts.json`` 是否存在且够用：

    * 没有该文件 → 报告里写明决策层不可用，并给出**具体原因**（不是一句「缺标注」）；
    * 有且够用 → 把人工结论填进样本，``verdict_layer_available`` 置 true。
    """
    from ai_service.eval.verdicts import DEFAULT_VERDICTS_PATH, load_verdicts

    samples = build_golden_set(labels_path, target_size=target_size)
    verdicts = load_verdicts(
        Path(verdicts_path) if verdicts_path is not None else DEFAULT_VERDICTS_PATH
    )

    sample_ids = [sample.sample_id for sample in samples]
    samples, attached = apply_human_verdicts(samples, verdicts)
    ready = verdicts.is_ready(sample_ids)

    notes = [
        "labels.json 只标字段真值与注入的退化类型，不含审核结论。",
        "任务层使用「原因码集合匹配率」作为代理指标，报告中标为 proxy。",
        "doc_type 已归一：id_card_front / id_card_back → id_card；"
        "application_form 不在审核链路上，已排除。",
        f"未映射的退化类型（{', '.join(UNMAPPED_QUALITY_TYPES)}）未纳入 golden 集。",
    ]
    if ready:
        notes.append(
            f"决策层已启用：{attached} 条样本带人工结论标注"
            f"（{verdicts.annotated_by or '未署名'}，{verdicts.annotated_at or '未注明日期'}）。"
        )
    else:
        for reason in verdicts.blocking_reasons([s.sample_id for s in samples]):
            notes.append(f"决策层不可用：{reason}。")

    return GoldenSet(
        samples=samples,
        labels_path=str(labels_path),
        verdict_layer_available=ready,
        notes=notes,
    )


def attach_verdicts(
    samples: Sequence[GoldenSample], verdicts: Any
) -> Tuple[List[GoldenSample], int]:
    """把人工结论附加到样本上，返回（新样本列表，成功附加的条数）。

    ``GoldenSample`` 是 **frozen** dataclass —— 直接改字段会抛
    ``FrozenInstanceError``。用 ``dataclasses.replace`` 生成新对象：
    不可变带来的好处（样本不会被下游悄悄改掉）值得多这一次拷贝。

    只附加「文件可用」的标注。不可用时原样返回，
    免得半份标注在半路上被当成完整基准用。
    """
    from dataclasses import replace

    if not verdicts.records:
        return list(samples), 0

    attached = 0
    updated: List[GoldenSample] = []
    for sample in samples:
        record = verdicts.records.get(sample.sample_id)
        if record is None:
            updated.append(sample)
            continue
        updated.append(
            replace(
                sample,
                expected_verdict=record.expected_verdict,
                verdict_source="human",
            )
        )
        attached += 1
    return updated, attached


def apply_human_verdicts(
    samples: Sequence[GoldenSample],
    verdicts: Any,
) -> Tuple[List[GoldenSample], int]:
    """只有标注集**可用**时才附加；否则原样返回。

    这是 ``load_golden_set`` 用的入口。为什么加一道可用性闸门：
    一份填了 5 条、没署名、格式还有错的标注，如果照单附加，
    决策层会基于 5 条算出个「正确率」并混进报告 —— 那正是
    ``MIN_READY_SAMPLES`` 想拦住的事。闸门放在这一处，
    比在每个消费方各判一次可靠。
    """
    sample_ids = [sample.sample_id for sample in samples]
    if not verdicts.is_ready(sample_ids):
        return list(samples), 0
    return attach_verdicts(samples, verdicts)


__all__ = (
    "DEFAULT_LABELS_PATH",
    "DEFAULT_TARGET_SIZE",
    "MAX_GOLDEN_SIZE",
    "QUALITY_TYPE_TO_REASONS",
    "UNMAPPED_QUALITY_TYPES",
    "GoldenSample",
    "GoldenSet",
    "apply_human_verdicts",
    "attach_verdicts",
    "build_golden_set",
    "expected_tools_for",
    "load_golden_set",
    "sample_balance",
)
