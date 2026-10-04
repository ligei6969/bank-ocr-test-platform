"""CMD-compatible entry point for prospective, experience-driven CTE."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ai_service.llm import build_llm_client
from test_evolution.discovery import DiscoveryEngine, export_regression
from test_evolution.discovery_contracts import Budget, TaskSpec
from test_evolution.discovery_store import DiscoveryStore, checksum

ROOT = Path(__file__).resolve().parents[1]


class DemoPredictor:
    """Explicit deterministic fixture; never presented as a live AI result."""
    available = True
    name = "fixture:cte-demo"

    async def complete(self, prompt: str, **kwargs) -> str:
        return json.dumps({"tests": [
            {"test_id": "plain", "pattern": "ascii_digits", "hypothesis": "Synthetic ASCII digits satisfy the output format invariant",
             "predicted_failure": False, "input": {"text": "TEST BANK 1111222233334444", "mutation": "none"}},
            {"test_id": "unicode", "pattern": "unicode_digits", "hypothesis": "Unicode digit matching may bypass an ASCII output contract",
             "predicted_failure": True, "input": {"text": "TEST BANK １１１１２２２２３３３３４４４４", "mutation": "none"}}
        ]}, ensure_ascii=False)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", type=Path, default=ROOT / "reports/cte-discovery")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "compare"):
        item = sub.add_parser(name)
        item.add_argument("--task", type=Path, required=True)
        mode = item.add_mutually_exclusive_group(required=True)
        mode.add_argument("--live", action="store_true", help="Explicit real model call, may incur API cost")
        mode.add_argument("--demo", action="store_true", help="Scripted fixture for engineering checks only")
        item.add_argument("--max-tests", type=int, default=6)
        item.add_argument("--max-output-tokens", type=int, default=3000)
        if name == "run":
            item.add_argument("--rounds", type=int, default=1)
            item.add_argument("--no-memory", action="store_true")
    item = sub.add_parser("propose")
    item.add_argument("--run", required=True)
    item.add_argument("--test", required=True)
    item.add_argument("--lesson", required=True)
    item.add_argument("--preconditions", required=True)
    item.add_argument("--scope", required=True)
    item.add_argument("--operation", choices=["observe", "generalize", "specialize", "merge", "add_counterexample"], default="observe")
    item.add_argument("--parent", action="append", default=[])
    item.add_argument("--counterexample", action="append", default=[])
    for name in ("oracle-review", "annotate"):
        item = sub.add_parser(name)
        item.add_argument("--run", required=True)
        item.add_argument("--test", required=True)
        if name == "annotate":
            item.add_argument("--expected-file", type=Path, required=True, help="JSON value, independently determined")
            item.add_argument("--reviewer", required=True)
            item.add_argument("--reference", required=True)
    item = sub.add_parser("promote")
    item.add_argument("--experience", required=True)
    item.add_argument("--reviewer", required=True)
    item.add_argument("--digest", required=True)
    item = sub.add_parser("reject")
    item.add_argument("--experience", required=True)
    item.add_argument("--reviewer", required=True)
    item.add_argument("--digest", required=True)
    item.add_argument("--reason", required=True)
    item = sub.add_parser("invalidate")
    item.add_argument("--experience", required=True)
    item.add_argument("--reviewer", required=True)
    item.add_argument("--reason", required=True)
    item = sub.add_parser("export")
    item.add_argument("--experience", required=True)
    item = sub.add_parser("import-legacy")
    item.add_argument("--candidate", required=True)
    item.add_argument("--target", choices=["card_number", "expiry", "id_fields"], required=True)
    item.add_argument("--group", required=True)
    item.add_argument("--lesson", required=True)
    item.add_argument("--reviewer", required=True)
    sub.add_parser("list")
    return p


def main(argv: list[str] | None = None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    store = DiscoveryStore(args.store)
    try:
        if args.command in {"run", "compare"}:
            task = TaskSpec.model_validate_json(args.task.read_text(encoding="utf-8"))
            if args.demo and task.target != "card_number":
                raise ValueError("Demo fixture only supports the synthetic card_number task")
            llm = DemoPredictor() if args.demo else build_llm_client()
            engine = DiscoveryEngine(store, llm, Budget(max_tests=args.max_tests, max_output_tokens=args.max_output_tokens))
            if args.command == "compare":
                report = asyncio.run(engine.compare(task))
                output = {"comparison_id": report["comparison_id"], "memory_count": report["memory_count"],
                          "A": report["A_no_memory"]["metrics"], "B": report["B_verified_memory"]["metrics"],
                          "conclusion": report["conclusion"]}
            else:
                results = asyncio.run(engine.run(task, use_memory=not args.no_memory, rounds=args.rounds))
                output = [{"run_id": r["run_id"], "metrics": r["metrics"],
                           "tests": [{"test_id": t["test_id"], "verdict": t["evaluation"]["verdict"]} for t in r["tests"]]}
                          for r in results]
        elif args.command == "oracle-review":
            output = store.oracle_review(args.run, args.test)
        elif args.command == "annotate":
            output = store.annotate(args.run, args.test, expected=json.loads(args.expected_file.read_text(encoding="utf-8")),
                                    reviewer=args.reviewer, reference=args.reference)
        elif args.command == "propose":
            exp = store.propose_experience(args.run, args.test, lesson=args.lesson, preconditions=args.preconditions,
                                          scope=args.scope, operation=args.operation, parents=args.parent,
                                          counterexamples=args.counterexample)
            output = {"experience": exp.model_dump(), "review_digest": checksum(exp.model_dump())}
        elif args.command == "promote":
            output = store.promote(args.experience, reviewer=args.reviewer, expected_digest=args.digest).model_dump()
        elif args.command == "reject":
            store.reject(args.experience, reviewer=args.reviewer, expected_digest=args.digest, reason=args.reason)
            output = {"status": "rejected", "experience_id": args.experience}
        elif args.command == "invalidate":
            store.invalidate(args.experience, reviewer=args.reviewer, reason=args.reason)
            output = {"status": "invalidated", "experience_id": args.experience}
        elif args.command == "export":
            output = {"regression_file": str(export_regression(store, args.experience))}
        elif args.command == "import-legacy":
            output = store.import_legacy(args.candidate, target=args.target, group=args.group, lesson=args.lesson,
                                         reviewer=args.reviewer, legacy_root=ROOT / "test_evolution")
        else:
            output = [e.model_dump() for e in store.active_experiences()] + store.active_seeds()
        print(json.dumps({"store": str(store.root), "result": output}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Existing LLM errors can contain provider details; do not print credentials.
        message = str(exc)
        import os
        for key in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
            secret = os.environ.get(key)
            if secret:
                message = message.replace(secret, "[REDACTED]")
        print(json.dumps({"error": type(exc).__name__, "message": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
