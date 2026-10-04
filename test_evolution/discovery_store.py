"""Append-only evidence and reviewed experience revisions for prospective CTE."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from test_evolution.discovery_contracts import Experience


def checksum(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:16]}"


class DiscoveryStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_/-]+\.json", name) or ".." in name or name.startswith("/"):
            raise ValueError("Invalid artifact path")
        result = self.root / name
        if not result.resolve().is_relative_to(self.root):
            raise ValueError("Artifact path escapes storage")
        return result

    def put(self, name: str, data: Any) -> str:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        sha = checksum(data)
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"sha256": sha, "data": data}, stream, ensure_ascii=False, indent=2)
        return sha

    def get(self, name: str) -> Any:
        packet = json.loads(self.path(name).read_text(encoding="utf-8"))
        if packet["sha256"] != checksum(packet["data"]):
            raise ValueError(f"Artifact integrity failure: {name}")
        return packet["data"]

    def list(self, folder: str) -> list[Any]:
        return [self.get(p.relative_to(self.root).as_posix())
                for p in sorted((self.root / folder).glob("*.json"))]

    def verify_run(self, run_id: str) -> tuple[dict, dict]:
        frozen = self.get(f"runs/{run_id}/prediction.json")
        result = self.get(f"runs/{run_id}/result.json")
        if result["prediction_digest"] != checksum(frozen):
            raise ValueError("Result does not belong to the frozen prediction")
        return frozen, result

    def known_inputs(self) -> set[tuple[str, str]]:
        known = set()
        for path in (self.root / "runs").glob("*/result.json"):
            frozen, result = self.verify_run(path.parent.name)
            for row in result["tests"]:
                known.add((frozen["public_task"]["target"], row["input_digest"]))
        return known

    def verify_experience(self, exp: Experience) -> None:
        frozen, result = self.verify_run(exp.evidence_run)
        if checksum(result) != exp.evidence_digest:
            raise ValueError("Experience evidence changed")
        if frozen["partition"] != "learning":
            raise ValueError("Validation/holdout results cannot enter the experience store")
        if not result["code_unchanged"] or not result["prospective"]:
            raise ValueError("Unstable code or replay cannot become verified experience")
        rows = [r for r in result["tests"] if r["test_id"] == exp.evidence_test]
        if len(rows) != 1 or rows[0]["duplicate"] or rows[0]["seen_before"]:
            raise ValueError("Missing, duplicate or previously observed evidence")
        row = rows[0]
        if exp.annotation_digest:
            annotation = self.get(f"annotations/{exp.evidence_run}/{exp.evidence_test}.json")
            if checksum(annotation) != exp.annotation_digest or annotation["result_digest"] != checksum(result):
                raise ValueError("Human annotation does not match execution evidence")
            verdict = annotation["verdict"]
        else:
            verdict = row["evaluation"]["verdict"]
        expected = "confirmed" if exp.verdict == "confirmed" else "passes_oracle"
        if verdict != expected:
            raise ValueError("Oracle does not support this experience verdict")
        if exp.target != frozen["public_task"]["target"] or exp.source_group != frozen["group"]:
            raise ValueError("Experience scope does not match evidence")
        if exp.source_version != checksum(frozen["code_version"]):
            raise ValueError("Experience code version does not match evidence")
        groups = {exp.source_group}
        for parent_id in exp.parents:
            parent = Experience.model_validate(self.get(f"verified/{parent_id}.json"))
            if parent.target != exp.target:
                raise ValueError("Cross-target generalization needs a separate reviewed task")
            groups.update(parent.source_groups)
        if set(exp.source_groups) != groups:
            raise ValueError("Experience lineage omits source groups")

    def oracle_review(self, run_id: str, test_id: str) -> dict:
        frozen, result = self.verify_run(run_id)
        test = next(t for t in frozen["tests"] if t["test_id"] == test_id)
        row = next(r for r in result["tests"] if r["test_id"] == test_id)
        return {"task": frozen["public_task"], "input": test["input"],
                "observation": row["observation"], "result_digest": checksum(result)}

    def annotate(self, run_id: str, test_id: str, *, expected: Any, reviewer: str, reference: str) -> dict:
        review = self.oracle_review(run_id, test_id)
        observed = review["observation"]
        if not reviewer.strip() or not reference.strip() or not observed["valid"] or not observed["stable"]:
            raise ValueError("Human oracle requires a reviewer, reference and stable execution")
        annotation = {"result_digest": review["result_digest"], "input_digest": checksum(review["input"]),
                      "expected": expected, "reviewer": reviewer, "reference": reference, "kind": "O3",
                      "verdict": "passes_oracle" if observed["outputs"][0]["variant"] == expected else "confirmed",
                      "at": now(), "timing": "post_execution_human_review_not_blind_prediction"}
        self.put(f"annotations/{run_id}/{test_id}.json", annotation)
        return annotation

    def active_experiences(self) -> list[Experience]:
        inactive = {e["experience_id"] for e in self.list("lifecycle")}
        result = []
        for raw in self.list("verified"):
            exp = Experience.model_validate(raw)
            if exp.experience_id in inactive:
                continue
            if exp.status != "verified" or not exp.reviewer.strip():
                raise ValueError("Unreviewed record in verified experience store")
            self.verify_experience(exp)
            result.append(exp)
        return result

    def import_legacy(self, candidate_id: str, *, target: str, group: str, lesson: str,
                      reviewer: str, legacy_root: Path) -> dict:
        """Review a generalized seed without pretending history was a blind prediction."""
        from test_evolution.schema import Candidate
        if not re.fullmatch(r"CTE-\d+", candidate_id) or target not in {"card_number", "expiry", "id_fields"}:
            raise ValueError("Invalid legacy candidate or target")
        if not all(value.strip() for value in (reviewer, group, lesson)):
            raise ValueError("A generalized lesson, family group and reviewer are required")
        source = legacy_root / "candidates" / f"{candidate_id}.json"
        knowledge = legacy_root / "validated" / f"{candidate_id}.md"
        raw = json.loads(source.read_text(encoding="utf-8"))
        candidate = Candidate.from_dict(raw)
        if candidate.status != "validated" or not candidate.is_elevated or not knowledge.is_file():
            raise ValueError("Only approved, validated historical evidence may seed retrieval")
        seed = {"experience_id": identifier("SEED"), "status": "verified", "target": target,
                "pattern": candidate.title, "preconditions": "Historical experience; check applicability to current code",
                "applicable_scope": "Human-reviewed transfer to " + target, "lesson": lesson,
                "verdict": "historical_evidence", "counterexamples": [], "source_version": "historical-not-prospective",
                "source_groups": [group], "reviewer": reviewer, "source_candidate": candidate_id,
                "source_path": str(source.resolve()), "knowledge_path": str(knowledge.resolve()),
                "source_digest": checksum(raw), "knowledge_digest": hashlib.sha256(knowledge.read_bytes()).hexdigest()}
        self.put(f"seeds/{seed['experience_id']}.json", seed)
        return seed

    def active_seeds(self) -> list[dict]:
        inactive = {e["experience_id"] for e in self.list("lifecycle")}
        seeds = []
        for seed in self.list("seeds"):
            if seed["experience_id"] in inactive:
                continue
            if seed["status"] != "verified" or not seed["reviewer"].strip():
                raise ValueError("Unreviewed legacy seed")
            if checksum(json.loads(Path(seed["source_path"]).read_text(encoding="utf-8"))) != seed["source_digest"]:
                raise ValueError("Historical source changed; re-review before retrieval")
            if hashlib.sha256(Path(seed["knowledge_path"]).read_bytes()).hexdigest() != seed["knowledge_digest"]:
                raise ValueError("Historical knowledge changed; re-review before retrieval")
            seeds.append(seed)
        return seeds

    def retrieve(self, target: str, query: str, excluded_group: str | None = None, limit: int = 5) -> list[dict]:
        def terms(text: str) -> set[str]:
            lowered = text.lower()
            return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2}", lowered)) | {
                lowered[i:i + 2] for i in range(len(lowered) - 1) if "\u4e00" <= lowered[i] <= "\u9fff"}
        wanted = terms(query)
        scored = []
        for exp in [e.model_dump() for e in self.active_experiences()] + self.active_seeds():
            if exp["target"] != target or excluded_group in exp["source_groups"]:
                continue
            score = len(wanted & terms(exp["pattern"] + " " + exp["lesson"] + " " + exp["applicable_scope"]))
            # Explicit whitelist: raw test input, labels and execution outputs stay out.
            public = {key: exp[key] for key in ("experience_id", "pattern", "preconditions", "lesson",
                      "applicable_scope", "verdict", "counterexamples", "source_version")}
            scored.append((score, exp["experience_id"], public))
        return [item[2] for item in sorted(scored, key=lambda x: (-x[0], x[1]))[:limit]]

    def propose_experience(self, run_id: str, test_id: str, *, lesson: str, preconditions: str,
                           scope: str, operation: str = "observe", parents: list[str] | None = None,
                           counterexamples: list[str] | None = None) -> Experience:
        frozen, result = self.verify_run(run_id)
        row = next(r for r in result["tests"] if r["test_id"] == test_id)
        annotation_path = f"annotations/{run_id}/{test_id}.json"
        annotation = self.get(annotation_path) if self.path(annotation_path).exists() else None
        verdict = annotation["verdict"] if annotation else row["evaluation"]["verdict"]
        groups = {frozen["group"]}
        for parent_id in parents or []:
            groups.update(self.get(f"verified/{parent_id}.json")["source_groups"])
        exp = Experience(experience_id=identifier("EXP"), target=frozen["public_task"]["target"],
                         pattern=row["pattern"], preconditions=preconditions, applicable_scope=scope, lesson=lesson,
                         verdict="confirmed" if verdict == "confirmed" else "counterexample",
                         evidence_run=run_id, evidence_test=test_id, evidence_digest=checksum(result),
                         annotation_digest=checksum(annotation) if annotation else "",
                         source_group=frozen["group"], source_groups=sorted(groups), source_version=checksum(frozen["code_version"]),
                         operation=operation, parents=parents or [], counterexamples=counterexamples or [])
        self.verify_experience(exp)
        if operation != "observe" and not exp.parents:
            raise ValueError("Experience evolution requires a parent revision")
        if operation == "merge" and len(set(exp.parents)) < 2:
            raise ValueError("Merge requires at least two parent experiences")
        active = {e.experience_id for e in self.active_experiences()}
        if set(exp.parents) - active:
            raise ValueError("Parent experience is not currently verified and active")
        self.put(f"candidates/{exp.experience_id}.json", exp.model_dump())
        return exp

    def promote(self, exp_id: str, *, reviewer: str, expected_digest: str) -> Experience:
        raw = self.get(f"candidates/{exp_id}.json")
        if self.path(f"rejected/{exp_id}.json").exists():
            raise ValueError("Rejected candidate is archived; propose a new revision")
        if not reviewer.strip() or checksum(raw) != expected_digest:
            raise ValueError("Reviewer and exact candidate digest are required")
        exp = Experience.model_validate(raw)
        self.verify_experience(exp)
        active = {e.experience_id for e in self.active_experiences()}
        if set(exp.parents) - active:
            raise ValueError("A parent was superseded; review the revision again")
        exp.status, exp.reviewer = "verified", reviewer.strip()
        self.put(f"verified/{exp_id}.json", exp.model_dump())
        for parent in exp.parents:
            self.invalidate(parent, reviewer=reviewer, reason=f"superseded_by:{exp_id}")
        return exp

    def reject(self, exp_id: str, *, reviewer: str, expected_digest: str, reason: str) -> None:
        raw = self.get(f"candidates/{exp_id}.json")
        if not reviewer.strip() or not reason.strip() or checksum(raw) != expected_digest:
            raise ValueError("Rejection requires reviewer, reason and exact candidate digest")
        if self.path(f"verified/{exp_id}.json").exists():
            raise ValueError("Already verified; use invalidate instead")
        self.put(f"rejected/{exp_id}.json", {"experience_id": exp_id, "candidate_digest": expected_digest,
                 "status": "rejected", "reviewer": reviewer.strip(), "reason": reason, "at": now()})

    def invalidate(self, exp_id: str, *, reviewer: str, reason: str) -> None:
        if not reviewer.strip() or not reason.strip():
            raise ValueError("Invalidation needs a reviewer and reason")
        self.get(f"{'seeds' if exp_id.startswith('SEED-') else 'verified'}/{exp_id}.json")
        self.put(f"lifecycle/{identifier('state')}.json", {"experience_id": exp_id, "reviewer": reviewer,
                 "reason": reason, "at": now()})
