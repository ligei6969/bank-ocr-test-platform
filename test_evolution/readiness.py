"""CTE Surface Readiness：哪些产品面现在就能做演进，哪些被数据缺口卡住。

为什么需要这张表
----------------
第一版方案原本的规则是「Phase 0 没完成 → CTE 不启动」。这条太粗暴：
BankOCR 现在不是一个单一系统，而它卡住的那个缺口（真实 OCR 字段）
只污染 OCR 与双判两个面。**知识面与威胁面的 Ground Truth 是确定性的
关键词判定，根本不碰 OCR** —— 拿它们去等数据真实化，是让整条 CTE
停在一个与它无关的前置条件上。

所以闸门按 surface 分：能做的先做（那条线上的第一个闭环立刻可跑），
卡住的如实标注，而不是假装整个系统都准备好了。

这张表是**声明**，不是权限
--------------------------
``readiness`` 不会阻止任何代码执行 —— 单人仓库里那种「阻止」只是自欺。
它的作用是：让 ``Event.learning_blocked`` 有个权威依据，
让报告里能写明「这条结论是在什么前提下得出的」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

#: 就绪级别。
#:
#: ``ready`` 与 ``partial`` 都允许学习（partial 的结论要写明前提），
#: 只有 ``blocked`` 会挡住 ``Event.learning_blocked``。
READY = "ready"
PARTIAL = "partial"
BLOCKED = "blocked"


@dataclass(frozen=True)
class Surface:
    """一个产品面的演进就绪度。"""

    name: str
    level: str
    ground_truth: str
    why: str
    exit_condition: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "level": self.level,
            "ground_truth": self.ground_truth,
            "why": self.why,
            "exit_condition": self.exit_condition,
        }


#: 五个面的就绪度。**这张表就是方案第九节的落地**。
SURFACES: Tuple[Surface, ...] = (
    Surface(
        name="knowledge",
        level=READY,
        ground_truth="确定性关键词规则表（intent 判定），人工可复核",
        why=(
            "判定是纯规则、可复现，「该不该拒答」有明确产品口径。"
            "不依赖 OCR 字段，也不依赖模型行为 —— CTE 可以有把握地说"
            "「这条写法漏拒了」，而不是「模型这次表现不好」。"
        ),
    ),
    Surface(
        name="threat",
        level=READY,
        ground_truth="45 条对外威胁集，每条都写明 expect 与 why",
        why=(
            "已有假想敌视角的用例集与明确 pass/fail。"
            "而且它**已经被证明能抓到东西** ——「我征信上有什么问题」"
            "就是它抓到的真实漏判（EVT-001），这比任何就绪度声明都有说服力。"
        ),
    ),
    Surface(
        name="agent",
        level=PARTIAL,
        ground_truth="工具白名单 + 轨迹断言（顺序无关的 tool_set_accuracy）",
        why=(
            "有白名单硬约束、轨迹断言和离线回放，可以做有限演进。"
            "但**「工具序列完全匹配」这个指标已被证明无效**"
            "（离线 1.0 vs 真实模型 0.225，实为同一组工具的顺序差异），"
            "所以基于它的学习结论不可靠。可用的判据是集合覆盖与安全不变式。"
        ),
        exit_condition="等真实模型多轮采样，把有效判据与噪声判据分开",
    ),
    Surface(
        name="ocr",
        level="partial",
        ground_truth="OCR 快照（`data/annotations/ocr_outputs.json`，50 条观测）",
        why=(
            "CTE-2 已交付真实 PaddleOCR 的 record/replay 快照，"
            "「字段全部解析成功」不再恒成立 —— 一录就暴露了三类真实缺陷"
            "（见 EVT-002 与 `docs/baseline_migrations/001_real_ocr_fields.md`）。"
            "但仍只标 partial 而非 ready：快照只覆盖 golden 用到的 50 张图，"
            "**降质样本（blur/glare 等）的字段错误率还没有系统化的基线**，"
            "而且刚暴露的解析缺陷尚未修复。"
        ),
        exit_condition="把 EVT-002 三条缺陷转成 CTE 事件走完闭环（CTE-3）",
    ),
    Surface(
        name="adjudication",
        level="partial",
        ground_truth="**受限** —— 结论受 fields 真实性制约，现已部分解除",
        why=(
            "安全不变式（规则能否被 LLM 推翻）一直照跑，是所有面里最扎实的部分。"
            "双判所缺的那个信号 ——「图像究竟还能不能读」—— CTE-2 的快照"
            "现在能给出一部分：反光样本里已经出现**单字符误识**"
            "（`5282448378463572` vs `...573`），正是双判该抓的那类错误。"
            "但快照样本量（50 张）还不足以标定改判正确性，所以标 partial。"
        ),
        exit_condition="扩大快照覆盖到降质样本全量，再标定双判改判正确性",
    ),
)


def surface(name: str) -> Surface:
    for item in SURFACES:
        if item.name == name:
            return item
    raise KeyError(f"未知 surface {name!r}；现有：{[s.name for s in SURFACES]}")


def learning_allowed(name: str) -> bool:
    """该面现在能不能据事件产出学习结论。

    ``partial`` 返回 ``True`` —— 有限演进总比不演进好，
    前提在报告里写明是哪个判据可用、哪个不可用。
    """
    return surface(name).level != BLOCKED


def readiness_table() -> str:
    """渲染成 markdown 表，供 README / skill 引用。"""
    lines = [
        "| Surface | 就绪度 | Ground Truth | 为什么 | 解除条件 |",
        "| --- | --- | --- | --- | --- |",
    ]
    mark = {READY: "✅ ready", PARTIAL: "🟡 partial", BLOCKED: "⛔ blocked"}
    for item in SURFACES:
        lines.append(
            f"| `{item.name}` | {mark[item.level]} | {item.ground_truth} | "
            f"{item.why} | {item.exit_condition or '—'} |"
        )
    return "\n".join(lines)


__all__ = (
    "BLOCKED",
    "PARTIAL",
    "READY",
    "SURFACES",
    "Surface",
    "learning_allowed",
    "readiness_table",
    "surface",
)
