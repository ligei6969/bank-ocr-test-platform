"""CTE 闭环的命令行入口。

用法::

    # 列出已登记的事件（内置的 + 已经跑过的）
    python -m scripts.run_cte --list

    # 跑一条事件的完整闭环（默认不晋级 —— 晋级要显式署名）
    python -m scripts.run_cte --event EVT-001

    # 带人工批准，跑完直接晋级
    python -m scripts.run_cte --event EVT-001 --approve jb

    # 只看某个历史版本上的重演结果
    python -m scripts.run_cte --replay "我征信上有什么问题"

退出码：
    0 —— 闭环跑完
    1 —— 事件不存在，或闭环某一步不满足晋级条件
    2 —— 参数不对
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _make_stdout_utf8() -> None:
    """Windows 默认码页是 GBK，中文结论直接打印会炸。与 evaluate_ai_review 同做法。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 取决于运行环境
            pass


_make_stdout_utf8()

from test_evolution.pipeline import EVENT_LOG, observe, run_event_loop  # noqa: E402
from test_evolution.readiness import learning_allowed  # noqa: E402
from test_evolution.replay import POLICY_HISTORY, current_behaviour, replay  # noqa: E402
from test_evolution.schema import (  # noqa: E402
    DEFAULT_EVOLUTION_DIR,
    EVENT_SURFACES,
    load_events,
)

#: 哪些事件的修复**已经落地**。
#:
#: 这不是「谁做的」的记账，而是验证时的必要输入：``passes_after_fix``
#: 这一步需要有一个「修复后的版本」可测。没有它，该步只能标 ``skipped``，
#: 候选也就无法完成机器验证。把「修复已落地」写成显式事实，
#: 比让验证去猜「现在算不算修好了」可靠。
FIX_LANDED = {"EVT-002", "EVT-004"}

#: 事件 → 它应该产出的 Candidate 类型与说明。
#: 写成表而不是让 CLI 现推：Candidate 的类型是人对复盘的判断，不是程序能猜的。
CANDIDATE_PLAN = {
    "EVT-001": {
        "candidate_id": "CTE-001",
        "candidate_type": "NEW_TEST",
        "candidate_title": "征信改写问句必须判为 pii",
        "proposed_change": (
            "新增回归测试，覆盖裸词「征信」族的改写写法"
            "（「我征信上有什么问题」「查一下征信」「征信哪里异常」），"
            "并断言这些写法在修复前版本上会被漏判。"
        ),
    },
    "EVT-002": {
        "candidate_id": "CTE-002",
        "candidate_type": "NEW_TEST",
        "candidate_title": "身份证字段解析必须容忍「标签与值分行」",
        "proposed_change": (
            "为 app/id_card_parser.py 补跨行取值：真实 PaddleOCR 把「姓名」与"
            "「沈梓欣」检测成两个独立文本框，而 _value_after_label 要求同一行。"
            "同类四处一并修：地址跨行收集、id_number 正则拒绝前导零、"
            "valid_period 只认同行。新增测试用真实 OCR 的分行序列做输入，"
            "断言解析器能取到 OCR 已经给出的字段。\n"
            "**CTE-3 已落地该修复**（人工决定后由开发提交），"
            "所以本次验证的 passes_after_fix 有可测对象。"
        ),
    },
    "EVT-003": {
        "candidate_id": "CTE-003",
        "candidate_type": "DOCUMENTATION",
        "candidate_title": "记录「OCR 未识别」与「解析失败」的区分方法",
        "proposed_change": (
            "不产出生产代码改动 —— 让 missing_* 原因码区分两种故障需要产品口径"
            "决策（是否对外暴露、算不算同一个指标）。本候选只把方法固定下来：\n"
            "1. CTE-2 的快照已录原始文本行，所以「证据在不在文本里」可计算；\n"
            "2. `tests/test_id_card_parser_real_ocr.py` 里的分布断言"
            "（2 张认出 / 8 张没认出）就是当前的事实基线；\n"
            "3. 归因前必须先做这一步，否则会把 OCR 的局限算成解析器的账。"
        ),
    },
    "EVT-004": {
        "candidate_id": "CTE-004",
        "candidate_type": "NEW_TEST",
        "candidate_title": "严重退化必须压过字段缺失",
        "proposed_change": (
            "调整 app/rule_check.py 的判定顺序：severe_reasons 先判，"
            "且不受 missing_reasons 影响；字段缺失项仍保留在原因码里。\n"
            "新增两条测试：严重度 + 字段全缺 → reject 且 severe 排在原因码最前；"
            "以及真实快照样本（variance 1.08）的回归。\n"
            "**CTE-3 已落地该修复**，并修掉了那条断言过弱、"
            "实际在为一个 bug 背书的旧测试。"
        ),
    },
}


def cmd_list(args: argparse.Namespace) -> int:
    registered = {spec["event_id"]: spec for spec in EVENT_LOG}
    for event in load_events(root=args.root):
        registered.setdefault(event.event_id, event.to_dict())

    if not registered:
        print("还没有登记任何事件。")
        return 0

    print(f"{'event_id':<10} {'surface':<12} {'source':<22} title")
    print("-" * 78)
    for event_id in sorted(registered):
        spec = registered[event_id]
        print(
            f"{event_id:<10} {spec['surface']:<12} {spec['source']:<22} {spec['title']}"
        )
    print()
    print(f"历史规则快照：{', '.join(sorted(POLICY_HISTORY))}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    print("当前 HEAD：")
    print(json.dumps(current_behaviour(args.replay).to_dict(), ensure_ascii=False, indent=2))
    for version in sorted(POLICY_HISTORY):
        print(f"\n{version}：")
        print(json.dumps(replay(args.replay, version).to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    spec = next((s for s in EVENT_LOG if s["event_id"] == args.event), None)
    if spec is None:
        print(f"没有登记事件 {args.event!r}。用 --list 看现有事件。", file=sys.stderr)
        return 1

    plan = CANDIDATE_PLAN.get(args.event)
    if plan is None:
        # 区分两种情况：还没配方案，还是**有意不配**（surface 被数据缺口卡住）。
        from test_evolution.readiness import learning_allowed, surface

        if spec["surface"] in EVENT_SURFACES and not learning_allowed(spec["surface"]):
            item = surface(spec["surface"])
            print(
                f"事件 {args.event!r} 落在 {spec['surface']} 面，该面当前 "
                f"**learning_blocked** —— 只登记事件，不产出 Candidate。\n"
                f"原因：{item.why}\n"
                f"解除条件：{item.exit_condition}",
                file=sys.stderr,
            )
            return 1
        print(
            f"事件 {args.event!r} 还没有配 Candidate 方案。\n"
            "Candidate 的类型是人对复盘的判断，不能在 CLI 里现推 —— "
            "请在 scripts/run_cte.py 的 CANDIDATE_PLAN 里补上。",
            file=sys.stderr,
        )
        return 2

    outcome = run_event_loop(
        spec,
        root=args.root,
        full_regression={"outcome": args.regression},
        approver=args.approve,
        fix_landed=args.event in FIX_LANDED,
        **plan,
    )

    print("=" * 78)
    print(f"事件       {outcome['event']['event_id']}  {outcome['event']['title']}")
    print(f"系统版本   {outcome['event']['system_version']}")
    print(f"盲预测     {outcome['prediction']['predicted_result']}"
          f"  (命中：{'是' if outcome['prediction']['hit'] else '否'})")
    print(f"实际       {outcome['execution']['actual_result']}"
          f"   期望 {outcome['comparison']['expected']}")
    print(f"分类       {outcome['comparison']['classification']}")
    print(f"复盘       {outcome['retro'] or '（未触发）'}")
    print("-" * 78)
    report = outcome["candidate"]
    print(f"Candidate  {report['candidate_id']}  [{report['type']}]  {report['status']}")
    for name, result in report["checks"].items():
        mark = {"pass": "✓", "fail": "✗", "skipped": "—"}.get(result, "·")
        print(f"  {mark} {name:<24} {result}")
    print(f"机器验证   {report['is_machine_validated']}")
    print(f"需人工批准 {report['needs_human_review']}")
    print(f"签名       {report['approver'] or '（未署名）'}")
    print("=" * 78)

    return 0


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="跑 CTE 闭环：Event → Predict → Execute → Compare → Reflect → Candidate → Validate"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_EVOLUTION_DIR,
                        help="CTE 证据目录（默认 test_evolution/）")
    parser.add_argument("--list", action="store_true", help="列出已登记事件")
    parser.add_argument("--event", help="要跑的事件 ID，如 EVT-001")
    parser.add_argument("--replay", metavar="QUESTION",
                        help="只做历史重演，给一个问题即可")
    parser.add_argument("--approve", default="",
                        help="人工批准人署名。不给则只跑到 machine_validated")
    parser.add_argument("--regression", default="pass", choices=["pass", "fail", "skipped"],
                        help="全量回归的结果（由调用方跑完后告知）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.list:
        return cmd_list(args)
    if args.replay:
        return cmd_replay(args)
    if args.event:
        return cmd_run(args)
    print("需要 --list / --event / --replay 之一。见 --help。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
