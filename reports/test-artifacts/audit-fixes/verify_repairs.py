"""Reproduce the old failures using captured source, without altering ground truth."""
import asyncio
import hashlib
import importlib.util
import json
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent

from app.field_parser import parse_bank_card_fields
from scripts.evaluate_external_readiness import evaluate, load_cases, summarize


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


old_parser = runpy.run_path(str(OUT / "field_parser_before.py"))
old_policy = load_module(OUT / "policy_before.py", "captured_policy")
policy_source = OUT / "policy_before.py"
(ROOT / "test_evolution/snapshots/policy_pre_evt007.json").write_text(json.dumps({
    "description": "EVT-007 修复前关键词表；只回放规则层，不代表历史模型",
    "commit": "working-tree-before-audit-fixes",
    "fixed_by": "audit-fixes-2026-09-28",
    "source_sha256": hashlib.sha256(policy_source.read_bytes()).hexdigest(),
    "intents": dict(old_policy._INTENT_RULES),
}, ensure_ascii=False, indent=2), encoding="utf-8")

labels = json.loads((ROOT / "data/annotations/labels.json").read_text(encoding="utf-8"))
observations = json.loads((ROOT / "data/annotations/ocr_outputs.json").read_text(encoding="utf-8"))["observations"]
stats, changed, regressions = {}, [], []
for label in labels:
    if label["doc_type"] != "bank_card" or label["image_path"] not in observations:
        continue
    observed = observations[label["image_path"]]
    text = "\n".join(observed["ocr_texts"])
    before = old_parser["parse_bank_card_fields"](text)
    after = parse_bank_card_fields(text)
    bucket = stats.setdefault(label["quality_type"], {"total": 0, "name_before": 0, "name_after": 0})
    bucket["total"] += 1
    expected = label["fields"]["name"]
    bucket["name_before"] += before["name"] == expected
    bucket["name_after"] += after["name"] == expected
    if before["name"] == expected and after["name"] != expected:
        regressions.append(label["image_path"])
    if before != after:
        changed.append({"path": label["image_path"], "expected_name": expected, "before": before, "after": after})

with patch("ai_service.knowledge.agent.policy.detect_intent", old_policy.detect_intent):
    before_outcomes = asyncio.run(evaluate(load_cases()))
summary = summarize(before_outcomes)
payload = {"parser": {"stats": stats, "regressions": regressions, "changed": changed},
           "old_policy_new_evaluator": {k: v for k, v in summary.items() if not isinstance(v, list)},
           "wrong_gate_questions": [o.question for o in summary["wrong_gate_refusals"]]}
(OUT / "before-after-proof.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"parser": stats, "regressions": regressions, "old_policy_new_evaluator": payload["old_policy_new_evaluator"]}, ensure_ascii=False, indent=2))
