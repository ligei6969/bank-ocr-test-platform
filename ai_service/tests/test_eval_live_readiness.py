"""Regression green must not masquerade as live-model validation."""

import asyncio
import json

from ai_service.eval.golden import load_golden_set
from ai_service.eval.report import evaluate
from ai_service.llm import NullLLMClient
from scripts import evaluate_ai_review as cli


def test_offline_report_explicitly_leaves_model_unverified(tmp_path):
    report = asyncio.run(evaluate(load_golden_set(target_size=4), baseline_path=None))
    assert report.passed
    assert report.execution["mode"] == "offline"
    assert not report.execution["real_model_verified"]
    assert not report.execution["live_validation_passed"]
    assert report.execution["model_quality_qualified"] is None
    cli.write_allure(report, tmp_path)
    results = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("*-result.json")]
    live = next(r for r in results if r["fullName"] == "ai-review.live-validation")
    assert live["status"] == "skipped"


def test_live_with_unavailable_model_fails_instead_of_silently_falling_back(monkeypatch):
    monkeypatch.setattr(cli, "build_llm_client", lambda: NullLLMClient())
    assert cli.main(["--live"]) == 2


def test_configured_but_failing_model_does_not_count_as_verified(monkeypatch):
    from ai_service.llm import LLMUnavailableError

    class FailingModel:
        available = True
        name = "unreachable-test-provider"

        async def complete(self, *args, **kwargs):
            raise LLMUnavailableError("test service unreachable")

    report = asyncio.run(evaluate(load_golden_set(target_size=4), llm=FailingModel(), baseline_path=None))
    assert report.passed
    assert report.execution["configured_model_available"]
    assert report.execution["fallback_samples"] == 4
    assert not report.execution["real_model_verified"]
    assert not report.execution["live_validation_passed"]


def test_live_cli_fails_even_when_baseline_passes_if_model_calls_failed(monkeypatch, tmp_path):
    from ai_service.llm import LLMUnavailableError

    class FailingModel:
        available = True
        name = "unreachable-test-provider"

        async def complete(self, *args, **kwargs):
            raise LLMUnavailableError("test service unreachable")

    monkeypatch.setattr(cli, "build_llm_client", lambda: FailingModel())
    monkeypatch.setattr(cli, "_platform_verdict_fn", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_platform_dual_judge_fn", lambda: None)
    path = tmp_path / "live.json"
    assert cli.main(["--live", "--no-baseline", "--target-size", "4", "--json", str(path)]) == 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["regression"]["passed"]
    assert not payload["execution"]["live_validation_passed"]
    assert payload["execution"]["judge_fallback_samples"] == 4
