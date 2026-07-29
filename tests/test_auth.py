"""Tests for login, session state, protected user pages, and logout."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.users import create_user


TEST_USERNAME = "auth-test-user"
TEST_PASSWORD = "Auth-test-password-123!"
LOGIN_ERROR_LOCATION = "/login?error=1"


def create_login_user(*, is_active: bool = True) -> dict:
    return create_user(
        username=TEST_USERNAME,
        password=TEST_PASSWORD,
        is_active=is_active,
    )


def login(
    client: TestClient,
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def test_login_page_contains_required_safe_controls(
    isolated_auth_client: TestClient,
) -> None:
    response = isolated_auth_client.get("/login")

    assert response.status_code == 200
    assert 'method="post"' in response.text
    assert 'action="/login"' in response.text
    assert 'name="username"' in response.text
    assert 'autocomplete="username"' in response.text
    assert 'name="password"' in response.text
    assert 'type="password"' in response.text
    assert 'autocomplete="current-password"' in response.text
    assert 'href="/static/portal/portal.css"' in response.text
    assert 'src="/static/portal/login.js"' in response.text
    for forbidden_text in ("test_user", "默认密码", "忘记密码", "短信验证码"):
        assert forbidden_text not in response.text


def test_login_script_uses_inline_safe_feedback(
    isolated_auth_client: TestClient,
) -> None:
    response = isolated_auth_client.get("/static/portal/login.js")

    assert response.status_code == 200
    assert "用户名或密码错误。" in response.text
    assert "登录中..." in response.text
    assert "textContent" in response.text
    assert "innerHTML" not in response.text
    assert "alert(" not in response.text
    assert "localStorage" not in response.text
    assert "sessionStorage" not in response.text


def test_successful_login_redirects_and_sets_minimal_session_cookie(
    isolated_auth_client: TestClient,
) -> None:
    user = create_login_user()

    response = login(isolated_auth_client)

    assert response.status_code == 303
    assert response.headers["location"] == "/user"
    set_cookie = response.headers["set-cookie"]
    assert "bank_ocr_session=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "secure" not in set_cookie.lower()

    cookie = isolated_auth_client.cookies.get("bank_ocr_session")
    assert cookie is not None
    encoded_payload = cookie.split(".", maxsplit=1)[0]
    session_data = json.loads(base64.b64decode(encoded_payload))
    assert session_data == {"user_id": user["id"]}


@pytest.mark.parametrize(
    "path",
    ["/user", "/user/bank-card", "/user/id-card"],
)
def test_authenticated_user_can_access_protected_pages(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    create_login_user()
    login(isolated_auth_client)

    response = isolated_auth_client.get(path, follow_redirects=False)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_authenticated_user_visiting_login_redirects_to_user_home(
    isolated_auth_client: TestClient,
) -> None:
    create_login_user()
    login(isolated_auth_client)

    response = isolated_auth_client.get("/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/user"


@pytest.mark.parametrize(
    ("scenario", "username", "password", "is_active"),
    [
        ("unknown", "missing-user", TEST_PASSWORD, None),
        ("wrong-password", TEST_USERNAME, "wrong-password", True),
        ("empty-username", "   ", TEST_PASSWORD, None),
        ("empty-password", TEST_USERNAME, "", True),
        ("inactive", TEST_USERNAME, TEST_PASSWORD, False),
    ],
)
def test_login_failures_share_one_response_and_create_no_valid_session(
    isolated_auth_client: TestClient,
    scenario: str,
    username: str,
    password: str,
    is_active: bool | None,
) -> None:
    if is_active is not None:
        create_login_user(is_active=is_active)

    response = login(
        isolated_auth_client,
        username=username,
        password=password,
    )

    assert scenario
    assert response.status_code == 303
    assert response.headers["location"] == LOGIN_ERROR_LOCATION
    assert isolated_auth_client.cookies.get("bank_ocr_session") is None
    protected = isolated_auth_client.get("/user", follow_redirects=False)
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"


@pytest.mark.parametrize(
    "path",
    ["/user", "/user/bank-card", "/user/id-card"],
)
def test_anonymous_user_is_redirected_from_protected_pages(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    response = isolated_auth_client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_session_for_deleted_user_is_cleared(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
) -> None:
    user = create_login_user()
    login(isolated_auth_client)
    with sqlite3.connect(auth_db_path) as connection:
        connection.execute("DELETE FROM users WHERE id = ?", (user["id"],))

    response = isolated_auth_client.get("/user", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert isolated_auth_client.cookies.get("bank_ocr_session") is None


def test_session_for_newly_inactive_user_is_cleared(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
) -> None:
    user = create_login_user()
    login(isolated_auth_client)
    with sqlite3.connect(auth_db_path) as connection:
        connection.execute(
            "UPDATE users SET is_active = 0 WHERE id = ?",
            (user["id"],),
        )

    response = isolated_auth_client.get("/user", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert isolated_auth_client.cookies.get("bank_ocr_session") is None


def test_post_logout_clears_session_and_redirects_to_login(
    isolated_auth_client: TestClient,
) -> None:
    create_login_user()
    login(isolated_auth_client)

    response = isolated_auth_client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    protected = isolated_auth_client.get("/user", follow_redirects=False)
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"


def test_get_logout_is_not_a_logout_endpoint(
    isolated_auth_client: TestClient,
) -> None:
    create_login_user()
    login(isolated_auth_client)

    response = isolated_auth_client.get("/logout", follow_redirects=False)

    assert response.status_code == 405
    assert isolated_auth_client.get("/user").status_code == 200


def test_user_pages_contain_post_logout_form(
    authenticated_client: TestClient,
) -> None:
    for path in ("/user", "/user/bank-card", "/user/id-card"):
        response = authenticated_client.get(path)
        assert 'method="post"' in response.text
        assert 'action="/logout"' in response.text


def test_admin_pages_redirect_anonymous_users_to_login(
    isolated_auth_client: TestClient,
) -> None:
    for path in ("/admin/reviews", "/admin/reviews/test-request-id"):
        response = isolated_auth_client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_existing_bank_card_debug_page_remains_unprotected(
    isolated_auth_client: TestClient,
) -> None:
    response = isolated_auth_client.get("/bank-card/ui", follow_redirects=False)

    assert response.status_code == 200
