"""Registered execution and independent, source-aware oracles.

The evaluator accepts no prediction, hypothesis, rationale or confidence.
O4/O5 mismatches are suspicions; O6 cannot produce a confirmed defect.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from test_evolution.discovery_contracts import OracleSpec, TestInput

TARGET_FILES = {"card_number": "app/field_parser.py", "expiry": "app/field_parser.py",
                "id_fields": "app/id_card_parser.py"}


def input_key(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def call_target(target: str, text: str) -> Any:
    from app.field_parser import extract_card_number, extract_valid_date
    from app.id_card_parser import parse_id_card_fields
    functions = {"card_number": extract_card_number, "expiry": extract_valid_date,
                 "id_fields": parse_id_card_fields}
    return functions[target](text)


def execute_input(target: str, value: dict[str, Any], repeats: int) -> dict[str, Any]:
    data = TestInput.model_validate(value)
    original = data.text
    transformed = {"none": original, "double_spaces": original.replace(" ", "  "),
                   "spaces_to_newlines": original.replace(" ", "\n")}[data.mutation]
    start = time.monotonic()
    try:
        outputs = [{"base": call_target(target, original), "variant": call_target(target, transformed)}
                   for _ in range(repeats)]
        stable = all(out == outputs[0] for out in outputs)
        return {"valid": True, "stable": stable, "outputs": outputs,
                "elapsed_ms": round((time.monotonic() - start) * 1000, 3)}
    except Exception as exc:
        return {"valid": False, "stable": False, "outputs": [],
                "error_type": type(exc).__name__, "elapsed_ms": round((time.monotonic() - start) * 1000, 3)}


def evaluate(oracle: OracleSpec, value: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    result = {"oracle_kind": oracle.kind, "reference": oracle.reference,
              "verdict": "inconclusive", "defect": None}
    if not observation.get("valid"):
        return {**result, "verdict": "execution_error"}
    if not observation.get("stable"):
        return {**result, "verdict": "unstable"}
    actual = observation["outputs"][0]["variant"]
    if oracle.kind == "O6":
        return {**result, "reason": "LLM judgment is advisory, not ground truth"}
    if oracle.rule == "relation":
        if value.get("mutation", "none") == "none":
            return {**result, "reason": "No transformation to compare"}
        mismatch = actual != observation["outputs"][0]["base"]
        return {**result, "verdict": "suspected" if mismatch else "relation_holds",
                "reason": "Relation checks do not establish semantic correctness"}
    if oracle.rule == "exact":
        key = input_key(value)
        if key not in oracle.labels:
            return {**result, "reason": "No independent answer for this generated input"}
        mismatch = actual != oracle.labels[key]
        if oracle.kind == "O4":
            return {**result, "verdict": "suspected" if mismatch else "reference_agrees"}
    elif actual is None:
        return {**result, "reason": "Format invariant cannot judge missing extraction"}
    elif oracle.rule == "number_format":
        mismatch = not isinstance(actual, str) or re.fullmatch(r"[0-9]{16,19}", actual) is None
    else:
        mismatch = not isinstance(actual, str) or re.fullmatch(r"(?:0[1-9]|1[0-2])/[0-9]{2}", actual) is None
    return {**result, "verdict": "confirmed" if mismatch else "passes_oracle", "defect": mismatch}
