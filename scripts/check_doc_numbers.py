"""核对文档里引用的全量测试数是否与登记的当前基线一致。

为什么需要这个脚本
------------------
这个项目的身份是「所有数字都是实测、每个结论都有适用范围」，但它自己的
文档里一度同时流通**六个不同的「当前测试数」**（1251 / 1263 / 1534 /
1539 / 1568 / 1581），只有一个是真的。最具体的伤害是
`docs/项目交接文档.md` 的交接检查清单写着「看到 1534 passed」——
接手的人照做会看到 1581，然后合理地怀疑自己搞坏了什么。

这类漂移不是靠「下次记得改」能解决的：每一轮开发都会新增测试，而文档
里引用基线的地方散布在七八个文件里。靠人手同步，漏一处就是一处假数字。

机制
----
`docs/baseline_numbers.yml` 是唯一登记处，分两类：

* ``current`` —— 必须与最近一次全量回归的实际数字一致；
* ``snapshots`` —— 历史快照，写明「当时」，**不追改、不参与对账**。
  保留它们是有意的：历史报告里的历史数字不是错误，假装它们应该跟着改
  才是错误。

脚本做的事：
1. 扫描白名单文件，抓取形如 ``1534 passed`` / ``1534 项`` 的「当前口径」声明；
2. 只把**未登记为快照**的数字视为「声称当前」，与 ``current`` 比对；
3. 同时验证登记表自己：``current.evidence`` 指的证据文件必须真的存在。

退出码：0 = 一致；1 = 有漂移（列出每一处）。CI 可直接当门禁用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "baseline_numbers.yml"

#: 声明「当前测试数」的文件白名单。
#:
#: 不做全仓库扫描的原因：`reports/` 下的历史证据、`test_evolution/` 的
#: 过程记录里也有大量数字，它们是**当时的观测**，不是「当前口径的声明」。
#: 把它们拉进对账，等于要求历史证据跟着现在的代码变 —— 那正好是这个
#: 项目明确反对的事（见 baseline_migrations 的立场）。
CHECKED_FILES: tuple[str, ...] = (
    "README.md",
    "docs/项目交接文档.md",
    "docs/项目验收报告.md",
    "docs/PR_描述.md",
    "docs/项目使用方法、亮点与成果.md",
    "docs/审查问题修复验证.md",
    "docs/CTE主动测试演进审核.md",
    "docs/SQLite写锁修复验证.md",
    "ai_service/README.md",
    "test_evolution/README.md",
)

#: 「当前口径」的措辞模式。
#:
#: 主模式：显式的时间/口径限定词 + ``N passed/项/条``。
#: 辅模式：**加粗强调**或**表格单元**里的 ``N passed`` —— 文档里几乎所有
#: 「这就是基线」的写法都带 ``**`` 或 ``|``，而历史叙事（「当时是 1052」、
#: 「修复前 1489」）不带。两道模式叠加比单纯放宽主模式更不容易误伤。
CURRENT_CLAIM = re.compile(r"[^\d](\d{3,5})\s*(?:passed|项|条)")
_CURRENT_CONTEXT = re.compile(
    r"(当前|最新|现在|期望|本次|目前|看到|验收|结果为|回归)"
)
_BOLD_OR_CELL = re.compile(r"\*\*\s*\d{3,5}\s*(?:passed|项|条)|\|\s*\d{3,5}\s*(?:passed|项|条)")

#: 历史叙事标记：出现这些词的行，其中的数字按快照对待。
SNAPSHOT_MARKERS = (
    "当时",
    "之前",
    "此前",
    "修复前",
    "历史",
    "原基线",
    "->",
    "→",
    "→ ",
    "增加 482",
)


def _load_registry() -> tuple[dict, list[dict]]:
    """极简读取 baseline_numbers.yml。

    刻意不引入 PyYAML 依赖（运行环境未必装了它），而这个文件的结构
    足够简单：顶层两个块，标量 + 缩进列表。真引入 YAML 解析器来读
    20 行配置，是把依赖当成了免费的。
    """
    import yaml  # type: ignore[import-not-found]

    payload = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    current = payload.get("current") or {}
    snapshots = payload.get("snapshots") or []
    return current, snapshots


def snapshot_numbers(snapshots: list[dict]) -> set[int]:
    """所有被登记为快照的数字 —— 对账时豁免。"""
    return {int(item["passed"]) for item in snapshots if item.get("passed")}


def extract_claims(text: str) -> list[tuple[int, str]]:
    """返回一行文本里所有「当前口径」的 (数字, 行内容)。

    两类命中：
    1. 行内有 ``当前/最新/期望/…`` 这类口径词，且有 ``N passed/项/条``；
    2. 行内 ``**N passed**`` 或表格单元 ``| N passed`` —— 加粗和表格是文档
       里「这就是基线」的标准写法。
    含历史标记（``当时/修复前/→`` …）的行整体跳过：那是快照叙事。
    """
    if any(marker in text for marker in SNAPSHOT_MARKERS):
        return []
    claims: list[tuple[int, str]] = []
    seen: set[int] = set()
    if _CURRENT_CONTEXT.search(text):
        for match in CURRENT_CLAIM.finditer(text):
            number = int(match.group(1))
            if number not in seen:
                seen.add(number)
                claims.append((number, text.strip()))
    for match in _BOLD_OR_CELL.finditer(text):
        number = int(re.search(r"\d{3,5}", match.group(0)).group(0))
        if number not in seen:
            seen.add(number)
            claims.append((number, text.strip()))
    return claims


def check() -> list[str]:
    """返回所有不一致的描述；空列表即通过。"""
    problems: list[str] = []

    try:
        current, snapshots = _load_registry()
    except FileNotFoundError:
        return [f"登记表不存在：{REGISTRY}"]
    except Exception as exc:  # noqa: BLE001 - 登记表坏了也算失败，要报清楚
        return [f"登记表无法解析（{exc}）—— 它是对账的唯一事实来源，必须先修好"]

    expected = current.get("passed")
    if not isinstance(expected, int):
        return ["登记表缺少 current.passed（整数）"]

    evidence = current.get("evidence")
    if evidence:
        if not (ROOT / evidence).exists():
            problems.append(
                f"current.evidence 指向的文件不存在：{evidence}"
                "（基线必须有可回放的证据）"
            )
    else:
        problems.append("current 缺少 evidence 字段 —— 没有证据的基线不是基线")

    exempt = snapshot_numbers(snapshots)
    exempt.add(expected)

    for relative in CHECKED_FILES:
        path = ROOT / relative
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            for number, _ in extract_claims(line):
                if number in exempt:
                    continue
                problems.append(
                    f"{relative}: 声称 {number}，登记的当前基线是 {expected}"
                    f"（且 {number} 未登记为历史快照）\n    > {line.strip()}"
                )
    return problems


def main() -> int:
    problems = check()
    if not problems:
        print("OK: 文档中的当前基线数字与登记表一致")
        return 0
    print(f"发现 {len(problems)} 处基线漂移：\n")
    for item in problems:
        print(f"  - {item}\n")
    print(
        "修复方式二选一：\n"
        "  1) 文档确实过时 → 改文档；\n"
        "  2) 文档写的是历史状态 → 在 docs/baseline_numbers.yml 的 snapshots\n"
        "     里补一条（写明 at/where/why），不要改历史正文。\n"
        "禁止的做法：为了让检查变绿去改登记表里的 current 数字 —— 那要先跑\n"
        "一次全量回归拿到真实数字。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
