# Repository Guidelines

## Project Structure & Module Organization

This is a Python OCR test platform for bank-card review flows. Core application code lives in `app/`: `main.py` defines the FastAPI app, `ocr_service.py` is the OCR integration layer, `quality_check.py` handles image checks, `field_parser.py` extracts OCR fields, and `rule_check.py` applies business rules. Tests live in `tests/` and follow pytest conventions. Data and generated assets are under `data/`, with annotations in `data/annotations/`; test outputs and temporary artifacts go to `reports/`. Utility data-generation scripts are in `scripts/`. Vendored code is isolated in `third_party/` and should not be edited unless the change is explicitly about that dependency.

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt`: install runtime and test dependencies.
- `python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`: run the API locally.
- `python -m pytest`: run the full test suite. `pytest.ini` already excludes large data, reports, caches, and vendored folders.
- `python scripts/generate_synthetic_bank_cards.py`: regenerate synthetic bank-card data when test fixtures need updating.

## Coding Style & Naming Conventions

Use Python 3 type hints and small, focused functions. Follow the existing style: four-space indentation, module docstrings, `snake_case` functions and variables, uppercase constants for regex patterns, and explicit return types where practical. Keep API response keys stable and descriptive, for example `review_result`, `quality`, `ocr_text`, and `fields`. Prefer `pathlib.Path` for filesystem paths in new code.

## Testing Guidelines

Tests use pytest and FastAPI `TestClient`. Name new files `test_*.py` and test functions `test_*`. Place generated test artifacts under `reports/test-artifacts/` rather than in source directories. Add or update tests when changing OCR parsing, image quality thresholds, review decisions, or endpoint contracts. For targeted checks, run examples such as `python -m pytest tests/test_bank_card_api.py`.

## Commit & Pull Request Guidelines

The repository currently has minimal Git history, so use concise imperative commit messages, for example `Add bank-card expiry validation`. Pull requests should include a short description, the commands run, and any changed fixture or generated-data rationale. Include screenshots or sample API responses when endpoint behavior or image outputs change.

## Security & Configuration Tips

Do not commit real customer documents, bank-card numbers, identity data, or secrets. Keep examples synthetic and clearly marked as test data. Large generated datasets and reports should remain in `data/` or `reports/`, not mixed into application modules.
