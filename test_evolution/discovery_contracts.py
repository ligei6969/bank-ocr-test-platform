"""Strict contracts for prospective CTE discovery, independent of historical replay."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OracleSpec(StrictModel):
    kind: Literal["O1", "O2", "O3", "O4", "O5", "O6"]
    rule: Literal["number_format", "expiry_format", "exact", "relation", "advisory"]
    reference: str = Field(min_length=1, max_length=2000)
    reviewed_by: str = ""
    # Sealed reference answers. This entire object stays out of model prompts.
    labels: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source(self) -> "OracleSpec":
        allowed = {"O1": {"number_format"}, "O2": {"expiry_format"},
                   "O3": {"exact"}, "O4": {"exact"}, "O5": {"relation"}, "O6": {"advisory"}}
        if self.rule not in allowed[self.kind]:
            raise ValueError("Oracle source and registered rule do not match")
        if self.kind == "O3" and not self.reviewed_by.strip():
            raise ValueError("O3 requires an independent human reviewer")
        return self


class TaskSpec(StrictModel):
    task_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    target: Literal["card_number", "expiry", "id_fields"]
    requirement: str = Field(min_length=1, max_length=6000)
    group: str = Field(min_length=1, max_length=100)
    partition: Literal["learning", "validation", "holdout"] = "learning"
    oracle: OracleSpec

    @model_validator(mode="after")
    def compatible_target(self) -> "TaskSpec":
        if self.oracle.rule == "number_format" and self.target != "card_number":
            raise ValueError("number_format requires card_number")
        if self.oracle.rule == "expiry_format" and self.target != "expiry":
            raise ValueError("expiry_format requires expiry")
        return self


class TestInput(StrictModel):
    text: str = Field(min_length=1, max_length=4096)
    mutation: Literal["none", "double_spaces", "spaces_to_newlines"] = "none"


class Hypothesis(StrictModel):
    test_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    pattern: str = Field(min_length=1, max_length=160)
    hypothesis: str = Field(min_length=1, max_length=1500)
    predicted_failure: bool
    input: TestInput
    # This is model self-report, never reported as a calibrated probability.
    confidence: float | None = Field(default=None, ge=0, le=1)


class PredictionBatch(StrictModel):
    tests: list[Hypothesis] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def distinct_ids(self) -> "PredictionBatch":
        ids = [t.test_id for t in self.tests]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate test IDs")
        return self


class Budget(StrictModel):
    max_tests: int = Field(default=6, ge=1, le=20)
    max_output_tokens: int = Field(default=3000, ge=256, le=8000)
    max_prompt_chars: int = Field(default=48000, ge=1000, le=100000)
    model_timeout_s: int = Field(default=90, ge=1, le=300)
    execution_timeout_s: int = Field(default=30, ge=1, le=120)
    repeats: int = Field(default=3, ge=2, le=5)


class Experience(StrictModel):
    experience_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    target: Literal["card_number", "expiry", "id_fields"]
    pattern: str = Field(min_length=1, max_length=160)
    preconditions: str = Field(min_length=1, max_length=2000)
    applicable_scope: str = Field(min_length=1, max_length=2000)
    lesson: str = Field(min_length=1, max_length=3000)
    verdict: Literal["confirmed", "counterexample"]
    status: Literal["tested", "verified"] = "tested"
    operation: Literal["observe", "generalize", "specialize", "merge", "add_counterexample"] = "observe"
    parents: list[str] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)
    evidence_run: str
    evidence_test: str
    evidence_digest: str
    annotation_digest: str = ""
    source_group: str
    source_groups: list[str] = Field(default_factory=list)
    source_version: str
    reviewer: str = ""
    # A failing test does not establish root cause. No model-made root-cause truth.
