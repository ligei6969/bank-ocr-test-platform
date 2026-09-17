"""人工审核结论标注：决策层指标的输入。

为什么这一格只能人填
--------------------
原因码的期望值可以从 ``labels.json`` 里注入的退化类型推导出来，所以任务层的
「原因码命中率」是机器算的。但**最终结论对错**推不出来 ——
`pass / review / reject` 是「按监管口径，这条记录该怎么处置」的判断，
取决于业务规则与风险偏好，不是从图像退化类型能算出来的量。

用代码推一份「期望结论」再拿它当基准，等于让系统自己给自己打分：
数字会很漂亮，但它测的是「系统是否自洽」，不是「系统是否对」。

所以这个文件是**唯一必须由人产出的评测输入**。代码能做的是把手工量压到最小：
用 ``scripts/make_verdict_worksheet.py`` 生成一张只差一列的清单，
标注人只需要填 `pass / review / reject`。

格式
----
```json
{
  "version": 1,
  "annotated_by": "张三（审核岗）",
  "annotated_at": "2026-09-15",
  "records": [
    {
      "sample_id": "golden-bank_card-blur-00",
      "expected_verdict": "review",
      "key_reason_codes": ["image_blur"],
      "note": "模糊但字段可读，按重拍处理"
    }
  ]
}
```

``annotated_by`` 与 ``annotated_at`` 是必填的：标注必须能追溯到**谁在什么时候标的**。
没有署名的标注集，在争议时无法复核，也就无法作为基准。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DEFAULT_VERDICTS_PATH = Path("data/annotations/review_verdicts.json")

VERDICTS_FORMAT_VERSION = 1

#: 允许的结论取值。与平台 ``review_records.review_result`` 的取值域一致。
VALID_VERDICTS: Tuple[str, ...] = ("pass", "review", "reject")

#: 启用决策层指标的最低要求。
#: 覆盖率不够就启用，会得到一个「基于 3 条样本的正确率」——
#: 那种数字比没有数字更糟，因为它看起来像个结论。
MIN_READY_SAMPLES = 20
MIN_READY_COVERAGE = 0.5


@dataclass(frozen=True)
class VerdictRecord:
    """一条人工结论标注。"""

    sample_id: str
    expected_verdict: str
    key_reason_codes: Tuple[str, ...] = ()
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "expected_verdict": self.expected_verdict,
            "key_reason_codes": list(self.key_reason_codes),
            "note": self.note,
        }


@dataclass
class VerdictSet:
    """一批标注 + 它的可用性判断。"""

    records: Dict[str, VerdictRecord] = field(default_factory=dict)
    path: str = str(DEFAULT_VERDICTS_PATH)
    annotated_by: str = ""
    annotated_at: str = ""
    #: 真正的格式错误（取值非法、id 重复等）。这些会导致整批不可用。
    issues: List[str] = field(default_factory=list)
    #: 还没填的行数。这不是错误，是「标注进行中」—— 单独计数，
    #: 否则 40 条未填会被报成「40 处格式问题」，把人往错的方向指。
    unfilled: int = 0
    #: 署名信息缺失项（谁标的 / 什么时候标的）
    signature_issues: List[str] = field(default_factory=list)
    present: bool = False

    def coverage_for(self, sample_ids: Iterable[str]) -> float:
        wanted = list(sample_ids)
        if not wanted:
            return 0.0
        hit = sum(1 for sample_id in wanted if sample_id in self.records)
        return hit / len(wanted)

    def is_ready(self, sample_ids: Sequence[str]) -> bool:
        """够不够启用决策层指标。

        四个条件：文件存在、格式没问题、有署名、条数与覆盖率达标。
        任何一个不满足都返回 False —— 宁可报告里写「决策层不可用」，
        也不要给出一个基于个位数样本的正确率。
        """
        if not self.present or self.issues or self.signature_issues:
            return False
        if len(self.records) < MIN_READY_SAMPLES:
            return False
        return self.coverage_for(sample_ids) >= MIN_READY_COVERAGE

    def blocking_reasons(self, sample_ids: Sequence[str]) -> List[str]:
        """为什么不可用。给报告用 —— 「不可用」必须能说出原因。

        每条原因都要能直接指导下一步动作：是「去填那一列」，
        还是「去修那一格」，还是「再多标几条」。一句笼统的
        「缺人工结论标注」等于让人自己猜。
        """
        if not self.present:
            return [f"标注文件不存在（{self.path}）"]

        reasons: List[str] = []
        if self.signature_issues:
            reasons.append("、".join(self.signature_issues) + "（标注必须可追溯）")
        if self.issues:
            shown = "；".join(self.issues[:3])
            more = f"，另有 {len(self.issues) - 3} 处" if len(self.issues) > 3 else ""
            reasons.append(f"格式问题 {len(self.issues)} 处：{shown}{more}")
        if self.unfilled:
            reasons.append(f"{self.unfilled} 条尚未填写 expected_verdict")
        if not self.signature_issues and not self.issues and len(self.records) < MIN_READY_SAMPLES:
            reasons.append(
                f"已完成 {len(self.records)} 条，少于门槛 {MIN_READY_SAMPLES} 条"
            )
        coverage = self.coverage_for(sample_ids)
        if not self.signature_issues and not self.issues and coverage < MIN_READY_COVERAGE:
            reasons.append(f"覆盖率 {coverage:.0%} 低于门槛 {MIN_READY_COVERAGE:.0%}")
        return reasons

    def summary(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "present": self.present,
            "records": len(self.records),
            "unfilled": self.unfilled,
            "annotated_by": self.annotated_by,
            "annotated_at": self.annotated_at,
            "issues": list(self.issues),
            "signature_issues": list(self.signature_issues),
        }


def load_verdicts(path: Path = DEFAULT_VERDICTS_PATH) -> VerdictSet:
    """读取标注文件。文件不存在不是错误 —— 只是决策层不可用。"""
    location = Path(path)
    if not location.is_file():
        return VerdictSet(path=str(location), present=False)

    try:
        payload = json.loads(location.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return VerdictSet(
            path=str(location),
            present=True,
            issues=[f"JSON 解析失败: {exc}"],
        )

    return parse_verdicts(payload, path=str(location))


def parse_verdicts(payload: Any, *, path: str = str(DEFAULT_VERDICTS_PATH)) -> VerdictSet:
    """把已解析的 JSON 变成 :class:`VerdictSet`，同时收集格式问题。

    刻意**不抛异常**：标注文件是人工产物，写错一格很常见。
    抛异常会让整个评测跑不起来，而正确做法是把它标成「不可用」并说明哪里不对 ——
    评测其它层照跑，人按提示去改那一格。
    """
    result = VerdictSet(path=path, present=True)

    if isinstance(payload, list):
        # 容忍裸列表：手写的文件经常省掉外层包装
        raw_records = payload
    elif isinstance(payload, Mapping):
        version = payload.get("version")
        if version is not None and version != VERDICTS_FORMAT_VERSION:
            result.issues.append(
                f"版本不匹配：文件是 {version}，当前支持 {VERDICTS_FORMAT_VERSION}"
            )
        result.annotated_by = str(payload.get("annotated_by") or "").strip()
        result.annotated_at = str(payload.get("annotated_at") or "").strip()
        raw_records = payload.get("records")
    else:
        result.issues.append("顶层必须是对象或数组")
        return result

    if not isinstance(raw_records, list):
        result.issues.append("records 必须是数组")
        return result

    if not result.annotated_by:
        result.signature_issues.append("缺少 annotated_by（谁标的）")
    if not result.annotated_at:
        result.signature_issues.append("缺少 annotated_at（什么时候标的）")

    for index, raw in enumerate(raw_records):
        if isinstance(raw, Mapping) and not str(raw.get("expected_verdict") or "").strip():
            # 未填是「标注进行中」，不是格式错误
            result.unfilled += 1
            continue
        record, problem = _parse_record(raw, index)
        if problem is not None:
            result.issues.append(problem)
            continue
        assert record is not None
        if record.sample_id in result.records:
            result.issues.append(f"sample_id 重复：{record.sample_id}")
            continue
        result.records[record.sample_id] = record

    return result


def _parse_record(
    raw: Any, index: int
) -> Tuple[Optional[VerdictRecord], Optional[str]]:
    if not isinstance(raw, Mapping):
        return None, f"第 {index} 条不是对象"

    sample_id = str(raw.get("sample_id") or "").strip()
    if not sample_id:
        return None, f"第 {index} 条缺少 sample_id"

    verdict = str(raw.get("expected_verdict") or "").strip().lower()
    if not verdict:
        # TODO 占位是工作表生成后的正常中间态，单独给一句明确的提示
        return None, f"{sample_id} 的 expected_verdict 还没填"
    if verdict not in VALID_VERDICTS:
        return None, (
            f"{sample_id} 的 expected_verdict 取值非法：{verdict!r}"
            f"（只能是 {'/'.join(VALID_VERDICTS)}）"
        )

    codes = raw.get("key_reason_codes") or []
    if isinstance(codes, str):
        codes = [codes]
    if not isinstance(codes, list):
        return None, f"{sample_id} 的 key_reason_codes 必须是数组"

    return (
        VerdictRecord(
            sample_id=sample_id,
            expected_verdict=verdict,
            key_reason_codes=tuple(str(item).strip() for item in codes if str(item).strip()),
            note=str(raw.get("note") or "").strip(),
        ),
        None,
    )


__all__ = (
    "DEFAULT_VERDICTS_PATH",
    "MIN_READY_COVERAGE",
    "MIN_READY_SAMPLES",
    "VALID_VERDICTS",
    "VERDICTS_FORMAT_VERSION",
    "VerdictRecord",
    "VerdictSet",
    "load_verdicts",
    "parse_verdicts",
)
