"""Shared pytest configuration for deterministic OCR mode selection."""

import os
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


os.environ.setdefault("SESSION_SECRET", "pytest-only-bank-ocr-session-secret")

from app.ai_client import set_ai_client  # noqa: E402
from app.main import app  # noqa: E402
from app.users import create_user  # noqa: E402


def get_csrf_token(client: TestClient) -> str:
    """Fetch the current Session-bound CSRF token through the public endpoint."""
    response = client.get("/csrf-token")
    assert response.status_code == 200
    token = response.json()["csrf_token"]
    assert isinstance(token, str)
    assert token
    return token


def login_with_csrf(
    client: TestClient,
    *,
    username: str,
    password: str,
):
    """Log in through the real CSRF flow and configure the rotated token."""
    token = get_csrf_token(client)
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )
    if response.status_code == 303 and response.headers["location"] == "/user":
        client.headers["X-CSRF-Token"] = get_csrf_token(client)
    return response


@pytest.fixture(autouse=True)
def isolate_ocr_mode(monkeypatch) -> None:
    """Prevent a developer shell OCR_MODE from changing test behavior."""
    monkeypatch.delenv("OCR_MODE", raising=False)


AI_ENV_VARS = (
    "AI_ASSIST_ENABLED",
    "AI_SERVICE_URL",
    "AI_ASSIST_TIMEOUT_S",
    "AI_ASSIST_FAILURE_THRESHOLD",
    "AI_ASSIST_RECOVERY_S",
)


@pytest.fixture(autouse=True)
def isolate_ai_assist(monkeypatch) -> Iterator[None]:
    """Keep AI assistant calls deterministic and offline during tests.

    这里**显式设 ``AI_ASSIST_ENABLED=false``**，而不是只删环境变量 ——
    该开关的默认值是 ``true``，光删变量等于「默认开启」：本地恰好起着
    AI 服务时，测试会真的把请求发出去，结果随环境而变。

    这个洞是被 P2.3 双判暴露的：一条断言 ``review_result`` 的记录查询测试
    在「本机没起 AI 服务」时靠快速失败蒙混过关，起了服务之后真实复核意见
    返回、结论随之改变，测试就红了 —— 而红的原因跟被测逻辑无关。

    清变量仍然要做（覆盖开发者 shell 里设的值），但必须把开关显式关掉，
    让「不联网」成为默认而不是巧合。
    """
    for name in AI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_ASSIST_ENABLED", "false")
    set_ai_client(None)
    yield
    set_ai_client(None)


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
    response = login_with_csrf(
        isolated_auth_client,
        username="portal-test-user",
        password=password,
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
    response = login_with_csrf(
        isolated_auth_client,
        username="portal-test-admin",
        password=password,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/user"
    yield isolated_auth_client
