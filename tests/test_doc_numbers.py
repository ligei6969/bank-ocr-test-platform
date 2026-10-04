"""文档基线数字的机械化对账。

背景
----
这个项目的身份是「所有数字都是实测、每个结论都有适用范围」，但它自己的
文档里一度同时流通**六个不同的「当前测试数」**（1251 / 1263 / 1534 /
1539 / 1568 / 1581）。伤害最具体的一处：`docs/项目交接文档.md` 的交接
检查清单写着「看到 1534 passed」，接手的人照做会看到 1581，然后合理地
怀疑自己搞坏了什么。

为什么做成测试而不是靠自觉：每一轮开发都会新增测试，而引用基线的地方
散布在近十个文档里。靠人手同步，漏一处就是一处假数字。

设计要点（同 ``scripts/check_doc_numbers.py``，这里是它的测试面）
----------------------------------------------------------------
1. **唯一事实来源**是 ``docs/baseline_numbers.yml``：
   - ``current`` —— 必须与最近一次全量回归一致；
   - ``snapshots`` —— 历史快照，**不追改、不参与对账**。
2. **历史数字不是错误**。报告里写「当时是 1052」是诚实的记录；
   假装历史数字应该跟着改，才是这个项目反对的事。所以豁免靠登记，
   而不是靠把历史正文改成新数字。
3. **登记表自己也要被测**：``current.evidence`` 指向的证据文件必须存在，
   否则基线就是一句没有出处的断言。

刻意不做的事：不解析 ``reports/`` 与 ``test_evolution/`` 下的过程证据。
它们是**当时的观测**，不是「当前口径的声明」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_doc_numbers import (
    CHECKED_FILES,
    REGISTRY,
    check,
    extract_claims,
    snapshot_numbers,
)

ROOT = Path(__file__).resolve().parents[1]


# ── 登记表自身的健康 ─────────────────────────────────────────────────────────

def test_baseline_registry_exists_and_is_well_formed() -> None:
    """登记表是对账的唯一事实来源，它坏了必须立刻知道。"""
    import yaml

    payload = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    current = payload["current"]
    assert isinstance(current["passed"], int), "current.passed 必须是整数"
    assert current["passed"] > 0
    assert current["recorded_at"], "必须记录基线产生日期"
    assert current["evidence"], "基线必须指向可回放的证据文件"

    snapshots = payload["snapshots"]
    assert snapshots, "至少要登记历史快照，否则豁免机制无从谈起"
    for item in snapshots:
        assert item["at"], f"快照 {item} 缺少 at（什么时候的数字）"
        assert item["passed"], f"快照 {item} 缺少 passed"
        assert item["where"], f"快照 {item} 缺少 where（出现在哪些文档）"
        assert item["why"], f"快照 {item} 缺少 why（那一轮做了什么）"


def test_current_baseline_is_not_registered_as_a_snapshot() -> None:
    """当前基线不该同时出现在快照里 —— 那会让「写对」和「写历史」无法区分。"""
    import yaml

    payload = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    current = payload["current"]["passed"]
    numbers = snapshot_numbers(payload["snapshots"])
    assert current not in numbers, (
        f"{current} 同时是 current 和 snapshot：先想清楚它到底是哪个口径"
    )


def test_current_baseline_evidence_file_exists() -> None:
    """没有证据文件的基线不是基线，是一句没有出处的断言。"""
    import yaml

    payload = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    evidence = payload["current"]["evidence"]
    assert (ROOT / evidence).exists(), f"current.evidence 不存在：{evidence}"


def test_snapshots_are_strictly_increasing_over_time() -> None:
    """测试数随时间只增不减（这是项目的硬承诺），登记表应能反映这一点。

    若你真的删除了测试，这里会红 —— 那时请认真核对：删除测试是否合规
    （项目规定不允许删测试、不允许放宽断言），还是登记表录错了。
    """
    import yaml

    payload = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    ordered = sorted(payload["snapshots"], key=lambda item: item["at"])
    numbers = [item["passed"] for item in ordered]
    for earlier, later in zip(numbers, numbers[1:]):
        assert later >= earlier, (
            f"登记表里 {ordered[numbers.index(later)]['at']} 的 {later} "
            f"小于之前的 {earlier} —— 测试数不该倒退"
        )


# ── 提取逻辑：能抓到，也不误伤 ────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # 「当前口径」的标准写法 —— 必须抓
        ("当前全量测试结果为 **1637 passed / 0 failed / 0 errors**", {1637}),
        ("| 全量自动化测试 | ✅ **1637 passed / 0 failed** |", {1637}),
        ("python -m pytest -q   # 期望 1637 passed", {1637}),
        ("- [ ] 能跑 pytest -q，看到 **1637 passed**", {1637}),
        ("展示 **1637 passed** 的全量文本报告", {1637}),
        # 历史叙事 —— 不能抓
        ("修复前已有 1489 条；本次新增 45 条后为 1534 条", set()),
        ("当时全量测试为 1052 passed", set()),
        ("原基线 1052，当前增加 482", set()),
        ("工具序列 1.0 → 0.225", set()),
        ("姓名 48 → 55", set()),
        ("blur 48->55", set()),
        # 与测试基线无关的数字 —— 不能抓
        ("失败率 2.57% / 23/896", set()),
        ("45/45 严格通过", set()),
    ],
)
def test_extract_claims_current_vs_historical(line: str, expected: set[int]) -> None:
    """提取器要分得清「当前口径」与「历史/无关数字」。"""
    got = {number for number, _ in extract_claims(line)}
    assert got == expected, f"{line!r} -> 提取到 {got}，期望 {expected}"


# ── 对账：真实文档 vs 登记表 ──────────────────────────────────────────────────

def test_real_documents_match_the_registered_baseline() -> None:
    """主门禁：白名单文档里的当前基线数字必须与登记表一致。

    失败时的两种修法（脚本输出里也写了）：
    1. 文档过时 → 改文档；
    2. 文档写的是历史状态 → 在 baseline_numbers.yml 补一条快照，
       **不要改历史正文**。
    禁止为了让这条变绿去改 current —— 那要先跑一次全量回归。
    """
    problems = check()
    assert problems == [], "文档基线漂移：\n" + "\n".join(problems)


def test_every_checked_file_still_exists() -> None:
    """白名单指向的文件必须存在。

    文件被改名/删除后不清理白名单，对账会静默少查一个文件 ——
    那比没有这个检查更糟：它给人「已经对过账」的错觉。
    """
    missing = [name for name in CHECKED_FILES if not (ROOT / name).exists()]
    assert missing == [], f"白名单里有不存在的文件：{missing}"


# ── 防退化：这个检查必须真的能抓漂移 ─────────────────────────────────────────

def test_check_catches_a_wrong_number(tmp_path, monkeypatch) -> None:
    """一个永远绿的检查等于没有检查。

    故意把一份假文档写进白名单，断言 check() 报告漂移。
    这是本测试存在的理由：如果有人改坏了提取逻辑，这条先红。
    """
    import yaml

    fake = tmp_path / "FAKE_DOC.md"
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    truth = registry["current"]["passed"]
    fake.write_text(
        f"当前全量测试结果为 **{truth + 999} passed / 0 failed**\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "scripts.check_doc_numbers.CHECKED_FILES", (str(fake.relative_to(ROOT)),)
    )
    problems = check()
    assert problems, "注入了 +999 的假基线却没被抓到 —— 检查失效了"
    assert str(truth + 999) in problems[0]


def test_check_accepts_a_registered_snapshot(tmp_path, monkeypatch) -> None:
    """登记过的历史数字出现在文档里不是错误 —— 豁免机制必须真的生效。"""
    import yaml

    fake = tmp_path / "HISTORY_DOC.md"
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    history = registry["snapshots"][0]["passed"]
    fake.write_text(
        f"当时全量测试为 {history} passed\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "scripts.check_doc_numbers.CHECKED_FILES", (str(fake.relative_to(ROOT)),)
    )
    problems = check()
    assert problems == [], f"历史快照 {history} 不该被报为漂移：{problems}"
