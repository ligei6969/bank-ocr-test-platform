"""Shared pytest configuration for deterministic OCR mode selection."""

import os
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("SESSION_SECRET", "pytest-only-bank-ocr-session-secret")

from app.main import app  # noqa: E402
from app.users import create_user  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_ocr_mode(monkeypatch) -> None:
    """Prevent a developer shell OCR_MODE from changing test behavior."""
    monkeypatch.delenv("OCR_MODE", raising=False)


@pytest.fixture
def auth_db_path(monkeypatch, tmp_path: Path) -> Iterator[Path]:
    """Use a per-test SQLite database for authentication-related requests."""
    database_path = tmp_path / "review_records.db"
    monkeypatch.setenv("REVIEW_RECORDS_DB_PATH", str(database_path))
    yield database_path
    database_path.unlink(missing_ok=True)


@pytest.fixture
def isolated_auth_client(auth_db_path: Path) -> Iterator[TestClient]:
    """Provide a cookie-capable client backed by an isolated user database."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def authenticated_client(
    isolated_auth_client: TestClient,
) -> Iterator[TestClient]:
    """Provide an authenticated user client for protected portal page tests."""
    password = "Portal-test-password-123!"
    create_user(username="portal-test-user", password=password)
    response = isolated_auth_client.post(
        "/login",
        data={"username": "portal-test-user", "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    yield isolated_auth_client


@pytest.fixture
def authenticated_admin_client(
    isolated_auth_client: TestClient,
) -> Iterator[TestClient]:
    """Provide an authenticated administrator for protected admin page tests."""
    password = "Portal-admin-password-123!"
    create_user(
        username="portal-test-admin",
        password=password,
        role="admin",
    )
    response = isolated_auth_client.post(
        "/login",
        data={"username": "portal-test-admin", "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/user"
    yield isolated_auth_client
