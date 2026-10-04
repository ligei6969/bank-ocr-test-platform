"""Run the same regression assertions against immutable pre-fix fixtures."""
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import pytest

mode = sys.argv[1]


class BeforeFixture:
    def pytest_collection_modifyitems(self, items):
        for item in items:
            if mode == "parser":
                old = runpy.run_path(str(ROOT / "test_evolution/snapshots/field_parser_pre_evt006.py"))
                item.module.parser = SimpleNamespace(**old)
            else:
                from test_evolution.replay import replay
                item.module.detect_intent = lambda question: replay(question, "policy@pre-EVT007").intent


path = "tests/test_bank_card_name_regression.py" if mode == "parser" else "ai_service/tests/test_boundary_paraphrases.py"
raise SystemExit(pytest.main([path, "-q", "-o", "addopts=-p no:cacheprovider"], plugins=[BeforeFixture()]))
