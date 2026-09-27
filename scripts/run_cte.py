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

from test_evolution.pipeline import EVENT_LOG, run_event_loop  # noqa: E402
from test_evolution.replay import POLICY_HISTORY, current_behaviour, replay  # noqa: E402
from test_evolution.schema import DEFAULT_EVOLUTION_DIR, load_events  # noqa: E402

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
