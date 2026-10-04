"""Prospective discovery must be evidence-driven and resistant to answer leakage."""

import asyncio
import inspect
import json
from pathlib import Path

import pytest

from ai_service.llm import NullLLMClient
from scripts.discover_cte import DemoPredictor, main
from test_evolution.discovery import DiscoveryEngine, export_regression, metrics
from test_evolution.discovery_contracts import Budget, OracleSpec, PredictionBatch, TaskSpec
from test_evolution.discovery_oracles import evaluate, execute_input, input_key
from test_evolution.discovery_store import DiscoveryStore, checksum


def task(**updates):
    payload = {"task_id": "test", "target": "card_number", "requirement": "ASCII digits only for extracted synthetic card numbers",
               "group": "family-a", "partition": "learning",
               "oracle": {"kind": "O1", "rule": "number_format", "reference": "test output contract"}}
    payload.update(updates)
    return TaskSpec.model_validate(payload)


class CaptureModel(DemoPredictor):
    def __init__(self):
        self.prompts = []

    async def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return await super().complete(prompt, **kwargs)


@pytest.fixture
def run(tmp_path):
    store, model = DiscoveryStore(tmp_path), CaptureModel()
    engine = DiscoveryEngine(store, model)
    result = asyncio.run(engine.run(task()))[0]
    return store, engine, model, result


def test_freeze_precedes_execution_and_never_contains_answers(tmp_path):
    store, model = DiscoveryStore(tmp_path), CaptureModel()
    engine = DiscoveryEngine(store, model)
    spec = task(oracle={"kind": "O3", "rule": "exact", "reviewed_by": "human", "reference": "SEALED-REFERENCE",
                        "labels": {"hidden": "SECRET-LABEL"}})
    run_id = asyncio.run(engine.predict(spec))
    assert "SECRET-LABEL" not in model.prompts[0]
    assert "SEALED-REFERENCE" not in model.prompts[0]
    assert not store.path(f"runs/{run_id}/result.json").exists()
    frozen = store.get(f"runs/{run_id}/prediction.json")
    engine.execute(spec, run_id)
    assert store.get(f"runs/{run_id}/prediction.json") == frozen
    with pytest.raises(FileExistsError):
        engine.execute(spec, run_id)


def test_model_cannot_supply_oracle_or_executable_code():
    payload = json.loads(asyncio.run(DemoPredictor().complete("")))
    payload["tests"][0]["expected"] = "fake answer"
    with pytest.raises(ValueError):
        PredictionBatch.model_validate(payload)
    payload["tests"][0].pop("expected")
    payload["tests"][0]["input"]["shell"] = "anything"
    with pytest.raises(ValueError):
        PredictionBatch.model_validate(payload)


def test_task_or_oracle_change_invalidates_prediction(tmp_path):
    engine = DiscoveryEngine(DiscoveryStore(tmp_path), DemoPredictor())
    run_id = asyncio.run(engine.predict(task()))
    with pytest.raises(ValueError, match="changed"):
        engine.execute(task(requirement="changed contract"), run_id)


def test_store_detects_corruption_and_refuses_overwrite(tmp_path):
    store = DiscoveryStore(tmp_path)
    store.put("a.json", {"value": 1})
    with pytest.raises(FileExistsError):
        store.put("a.json", {"value": 2})
    raw = json.loads(store.path("a.json").read_text())
    raw["data"]["value"] = 2
    store.path("a.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="integrity"):
        store.get("a.json")
    with pytest.raises(ValueError):
        store.put("../outside.json", {})


def test_no_model_is_not_mislabeled_as_ai(tmp_path):
    engine = DiscoveryEngine(DiscoveryStore(tmp_path), NullLLMClient())
    with pytest.raises(ValueError, match="requires a configured model"):
        asyncio.run(engine.run(task()))


def test_oracle_never_receives_predictor_reasoning():
    assert list(inspect.signature(evaluate).parameters) == ["oracle", "value", "observation"]
    assert "predicted_failure" not in inspect.getsource(evaluate)


@pytest.mark.parametrize("kind,rule", [("O4", "exact"), ("O5", "relation"), ("O6", "advisory")])
def test_weak_oracles_cannot_confirm_defects(kind, rule):
    value = {"text": "TEST", "mutation": "spaces_to_newlines"}
    spec = OracleSpec(kind=kind, rule=rule, reference="reference", labels={input_key(value): "expected"})
    observed = {"valid": True, "stable": True, "outputs": [{"base": "expected", "variant": "different"}]}
    result = evaluate(spec, value, observed)
    assert result["defect"] is None
    assert result["verdict"] in {"suspected", "inconclusive"}


def test_human_oracle_requires_independent_review_and_exact_input():
    with pytest.raises(ValueError):
        OracleSpec(kind="O3", rule="exact", reference="r")
    spec = OracleSpec(kind="O3", rule="exact", reference="r", reviewed_by="human", labels={})
    assert evaluate(spec, {"text": "X"}, {"valid": True, "stable": True, "outputs": [{"variant": "X"}]})["defect"] is None


def test_error_and_instability_are_not_defects():
    for observation in ({"valid": False}, {"valid": True, "stable": False}):
        assert evaluate(task().oracle, {}, observation)["defect"] is None


def test_real_registered_parser_and_independent_format_oracle(run):
    store, _, _, result = run
    rows = result["tests"]
    assert rows[0]["evaluation"]["verdict"] == "passes_oracle"
    # The test oracle checks the declared ASCII contract, not model expectations.
    assert rows[1]["evaluation"]["verdict"] == "confirmed"
    assert all(r["observation"]["stable"] for r in rows)
    assert result["metrics"]["confirmed_failing_inputs"] == 1
    assert result["metrics"]["novel_defect_family_count"] is None
    assert result["metrics"]["tokens_per_confirmed_failing_input"] is None


def make_experience(store, result, test="unicode", **kwargs):
    return store.propose_experience(result["run_id"], test, lesson="Unicode digits need an explicit output contract",
                                    preconditions="Synthetic Unicode digit text", scope="card_number format in recorded code version", **kwargs)


def test_only_digest_reviewed_experience_is_retrievable(run):
    store, _, _, result = run
    exp = make_experience(store, result)
    assert not store.retrieve("card_number", "Unicode")
    with pytest.raises(ValueError):
        store.promote(exp.experience_id, reviewer="human", expected_digest="wrong")
    approved = store.promote(exp.experience_id, reviewer="human", expected_digest=checksum(exp.model_dump()))
    found = store.retrieve("card_number", "Unicode")
    assert found[0]["experience_id"] == approved.experience_id
    assert "labels" not in found[0] and "outputs" not in found[0]
    assert store.retrieve("card_number", "Unicode", excluded_group="family-a") == []
    store.invalidate(exp.experience_id, reviewer="human", reason="scope no longer valid")
    assert not store.retrieve("card_number", "Unicode")
    assert store.get(f"verified/{exp.experience_id}.json")["status"] == "verified"  # historical record preserved


def test_counterexample_and_revision_supersede_without_overwriting(run):
    store, _, _, result = run
    first = make_experience(store, result)
    store.promote(first.experience_id, reviewer="human", expected_digest=checksum(first.model_dump()))
    second = make_experience(store, result, test="plain", operation="add_counterexample", parents=[first.experience_id],
                             counterexamples=["ASCII synthetic digits satisfy the format contract"])
    assert second.verdict == "counterexample"
    store.promote(second.experience_id, reviewer="human", expected_digest=checksum(second.model_dump()))
    assert [e.experience_id for e in store.active_experiences()] == [second.experience_id]


def test_rejected_candidate_is_archived_and_cannot_be_promoted(run):
    store, _, _, result = run
    exp = make_experience(store, result)
    digest = checksum(exp.model_dump())
    with pytest.raises(ValueError, match="digest"):
        store.reject(exp.experience_id, reviewer="test-human", expected_digest="wrong", reason="Overgeneralized")
    store.reject(exp.experience_id, reviewer="test-human", expected_digest=digest, reason="Lesson exceeds evidence")
    assert store.get(f"candidates/{exp.experience_id}.json") == exp.model_dump()
    assert store.get(f"rejected/{exp.experience_id}.json")["status"] == "rejected"
    with pytest.raises(ValueError, match="archived"):
        store.promote(exp.experience_id, reviewer="test-human", expected_digest=digest)
    assert not store.retrieve("card_number", "Unicode")


def test_repeated_inputs_are_not_new_blind_discoveries(run):
    store, engine, _, first = run
    second = asyncio.run(engine.run(task()))[0]
    assert second["metrics"]["unique_new_tests"] == 0
    with pytest.raises(ValueError, match="previously observed"):
        make_experience(store, second)


def test_holdout_cannot_feed_back_or_be_promoted(tmp_path):
    store = DiscoveryStore(tmp_path)
    engine = DiscoveryEngine(store, DemoPredictor())
    spec = task(partition="holdout")
    with pytest.raises(ValueError):
        asyncio.run(engine.run(spec, rounds=2))
    with pytest.raises(ValueError):
        asyncio.run(engine.predict(spec, feedback=[{"actual": "leak"}]))
    result = asyncio.run(engine.run(spec))[0]
    with pytest.raises(ValueError, match="cannot enter"):
        make_experience(store, result)


def test_ab_freezes_both_arms_before_execution(tmp_path, monkeypatch):
    engine = DiscoveryEngine(DiscoveryStore(tmp_path), CaptureModel())
    original = engine.execute
    calls = []
    def execute(*args, **kwargs):
        assert len(engine.llm.prompts) == 2
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(engine, "execute", execute)
    report = asyncio.run(engine.compare(task(partition="holdout")))
    assert len(calls) == 2
    assert report["A_no_memory"]["metrics"] ["previously_seen_rate"] == 0
    assert report["B_verified_memory"]["metrics"] ["previously_seen_rate"] == 0


def test_exported_regression_is_trusted_template_not_model_code(run):
    store, _, _, result = run
    exp = make_experience(store, result, test="plain")
    store.promote(exp.experience_id, reviewer="human", expected_digest=checksum(exp.model_dump()))
    path = export_regression(store, exp.experience_id)
    namespace = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    namespace["test_reviewed_cte_case"]()


def test_metric_denominators_do_not_conflate_false_positives_with_false_discovery():
    def row(predicted, defect):
        return {"duplicate": False, "seen_before": False, "predicted_failure": predicted,
                "evaluation": {"defect": defect}, "observation": {"valid": True, "stable": True}}
    report = metrics([row(True, True), row(True, False), row(False, False), row(False, False), row(False, None)], None, 1)
    assert report["false_positive_rate"] == pytest.approx(1 / 3)
    assert report["false_discovery_rate"] == 0.5
    assert report["determinate_tests"] == 4
    assert metrics([], None, 0)["valid_test_rate"] is None


def test_cli_requires_explicit_live_or_demo():
    with pytest.raises(SystemExit):
        main(["run", "--task", "unused.json"])


def test_shipped_tasks_obey_registered_contracts():
    examples = Path(__file__).resolve().parents[1] / "discovery_examples"
    specs = [TaskSpec.model_validate_json(path.read_text(encoding="utf-8")) for path in examples.glob("*.json")]
    assert {spec.target for spec in specs} == {"card_number", "expiry", "id_fields"}


def test_weak_result_can_receive_separate_human_annotation(tmp_path):
    store = DiscoveryStore(tmp_path)
    engine = DiscoveryEngine(store, DemoPredictor())
    spec = task(oracle={"kind": "O6", "rule": "advisory", "reference": "untrusted model opinion"})
    result = asyncio.run(engine.run(spec))[0]
    before = store.get(f"runs/{result['run_id']}/result.json")
    with pytest.raises(ValueError, match="does not support"):
        make_experience(store, result)
    review = store.oracle_review(result["run_id"], "unicode")
    assert "hypothesis" not in json.dumps(review) and "predicted_failure" not in json.dumps(review)
    store.annotate(result["run_id"], "unicode", expected="1111222233334444", reviewer="test-human",
                   reference="independently checked normalization contract")
    exp = make_experience(store, result)
    assert exp.annotation_digest
    assert store.get(f"runs/{result['run_id']}/result.json") == before
    store.promote(exp.experience_id, reviewer="test-human", expected_digest=checksum(exp.model_dump()))
    path = export_regression(store, exp.experience_id)
    assert "independently checked normalization contract" in path.read_text(encoding="utf-8")


def test_parent_groups_cannot_be_laundered_by_revision(run):
    store, _, _, result = run
    first = make_experience(store, result)
    store.promote(first.experience_id, reviewer="test-human", expected_digest=checksum(first.model_dump()))
    # A new task family, with a new text so it is not a replay of observed inputs.
    class NewInput(DemoPredictor):
        async def complete(self, prompt, **kwargs):
            data = json.loads(await super().complete(prompt, **kwargs))
            for test in data["tests"]:
                test["input"]["text"] += "\nSYNTHETIC VARIANT"
            return json.dumps(data)
    later = asyncio.run(DiscoveryEngine(store, NewInput()).run(task(group="family-b")))[0]
    second = make_experience(store, later, operation="specialize", parents=[first.experience_id])
    assert set(second.source_groups) == {"family-a", "family-b"}
    store.promote(second.experience_id, reviewer="test-human", expected_digest=checksum(second.model_dump()))
    assert not store.retrieve("card_number", "Unicode", excluded_group="family-a")


def test_legacy_import_is_reviewed_generalization_and_not_raw_outcomes(tmp_path):
    store = DiscoveryStore(tmp_path)
    legacy = Path(__file__).resolve().parents[1]
    seed = store.import_legacy("CTE-002", target="id_fields", group="id-layout", lesson="Cross-line labels need association checks",
                              reviewer="test-human", legacy_root=legacy)
    found = store.retrieve("id_fields", "labels")
    assert found[0]["experience_id"] == seed["experience_id"]
    assert "沈梓欣" not in json.dumps(found, ensure_ascii=False)
    assert found[0]["source_version"] == "historical-not-prospective"
    assert not store.retrieve("id_fields", "labels", excluded_group="id-layout")
    store.invalidate(seed["experience_id"], reviewer="test-human", reason="test invalidation")
    assert not store.retrieve("id_fields", "labels")


def test_over_budget_output_is_not_silently_truncated(tmp_path):
    engine = DiscoveryEngine(DiscoveryStore(tmp_path), DemoPredictor(), Budget(max_tests=1))
    with pytest.raises(ValueError, match="budget"):
        asyncio.run(engine.run(task()))
    assert not list(tmp_path.glob("runs/*/prediction.json"))
    assert list(tmp_path.glob("runs/*/model-error.json"))


def test_engine_stops_after_repeated_inputs(tmp_path):
    model = CaptureModel()
    result = asyncio.run(DiscoveryEngine(DiscoveryStore(tmp_path), model).run(task(), rounds=3))
    assert len(result) == 2
    assert "observed_verdict" in model.prompts[1]
    assert result[1]["metrics"]["unique_new_tests"] == 0
