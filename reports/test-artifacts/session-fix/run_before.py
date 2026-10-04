"""Run new session regressions with the repository's pre-fix key function."""
import ast
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import pytest
from app import main

source = subprocess.check_output(["git", "show", "HEAD:app/main.py"], cwd=ROOT, text=True, encoding="utf-8")
function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "_get_session_secret")
namespace = {"os": os, "secrets": secrets, "logger": main.logger}
exec(compile(ast.Module(body=[function], type_ignores=[]), "pre-fix-session-config", "exec"), namespace)


class BeforeKey:
    def pytest_collection_modifyitems(self, items):
        main._get_session_secret = namespace["_get_session_secret"]


raise SystemExit(pytest.main(["tests/test_session_persistence.py", "-q", "-o", "addopts=-p no:cacheprovider"], plugins=[BeforeKey()]))
