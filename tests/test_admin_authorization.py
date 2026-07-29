"""Authorization tests for administrator portal pages."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.users import create_user


USER_PASSWORD = "Normal-user-password-123!"
ADMIN_PASSWORD = "Admin-user-password-123!"
ADMIN_PATHS = (
    "/admin/reviews",
    "/admin/reviews/private-request-id",
)
USER_PATHS = (
    "/user",
    "/user/bank-card",
    "/user/id-card",
)


def get_csrf_token(client: TestClient) -> str:
    response = client.get("/csrf-token")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def create_account(
    *,
    username: str,
    password: str,
    role: str,
    is_active: bool = True,
) -> dict:
    return create_user(
        username=username,
        password=password,
        role=role,
        is_active=is_active,
    )


def login(client: TestClient, *, username: str, password: str):
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


def create_and_login_admin(client: TestClient) -> dict:
    user = create_account(
        username="authorization-admin",
        password=ADMIN_PASSWORD,
        role="admin",
    )
    response = login(
        client,
        username=user["username"],
        password=ADMIN_PASSWORD,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/user"
    return user


def create_and_login_user(client: TestClient) -> dict:
    user = create_account(
        username="authorization-user",
        password=USER_PASSWORD,
        role="user",
    )
    response = login(
        client,
        username=user["username"],
        password=USER_PASSWORD,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/user"
    return user


def test_admin_login_still_redirects_to_user_home_and_session_is_minimal(
    isolated_auth_client: TestClient,
) -> None:
    admin = create_and_login_admin(isolated_auth_client)

    cookie = isolated_auth_client.cookies.get("bank_ocr_session")
    assert cookie is not None
    encoded_payload = cookie.split(".", maxsplit=1)[0]
    session_data = json.loads(base64.b64decode(encoded_payload))

    assert session_data["user_id"] == admin["id"]
    assert isinstance(session_data["csrf_token"], str)
    assert set(session_data) == {"user_id", "csrf_token"}
    assert "role" not in cookie
    assert "password" not in cookie
    assert admin["password_hash"] not in cookie


def test_logged_in_admin_visiting_login_redirects_to_user_home(
    isolated_auth_client: TestClient,
) -> None:
    create_and_login_admin(isolated_auth_client)

    response = isolated_auth_client.get("/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/user"


def test_disabled_admin_cannot_log_in(
    isolated_auth_client: TestClient,
) -> None:
    create_account(
        username="disabled-admin",
        password=ADMIN_PASSWORD,
        role="admin",
        is_active=False,
    )

    response = login(
        isolated_auth_client,
        username="disabled-admin",
        password=ADMIN_PASSWORD,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=1"
    cookie = isolated_auth_client.cookies.get("bank_ocr_session")
    assert cookie is not None
    session_data = json.loads(base64.b64decode(cookie.split(".", maxsplit=1)[0]))
    assert set(session_data) == {"csrf_token"}


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_anonymous_user_is_redirected_from_admin_pages_without_page_content(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    response = isolated_auth_client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "审核记录列表" not in response.text
    assert "detailRequestId" not in response.text
    assert "private-request-id" not in response.text


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_normal_user_receives_safe_html_403_from_admin_pages(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    create_and_login_user(isolated_auth_client)

    response = isolated_auth_client.get(path, follow_redirects=False)

    assert response.status_code == 403
    assert "text/html" in response.headers["content-type"]
    assert "无权访问" in response.text
    assert "当前账号没有访问管理员后台的权限" in response.text
    assert 'href="/user"' in response.text
    assert 'method="post"' in response.text
    assert 'action="/logout"' in response.text
    assert "审核记录列表" not in response.text
    assert "detailRequestId" not in response.text
    assert "private-request-id" not in response.text


@pytest.mark.parametrize("path", USER_PATHS)
def test_normal_user_can_still_access_user_pages(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    create_and_login_user(isolated_auth_client)

    response = isolated_auth_client.get(path, follow_redirects=False)

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("path", "script_path"),
    [
        ("/admin/reviews", "/static/portal/admin_reviews.js"),
        (
            "/admin/reviews/private-request-id",
            "/static/portal/admin_review_detail.js",
        ),
    ],
)
def test_admin_can_access_admin_pages_with_existing_interaction_scripts(
    isolated_auth_client: TestClient,
    path: str,
    script_path: str,
) -> None:
    create_and_login_admin(isolated_auth_client)

    response = isolated_auth_client.get(path, follow_redirects=False)

    assert response.status_code == 200
    assert f'src="{script_path}"' in response.text


@pytest.mark.parametrize("path", USER_PATHS)
def test_admin_can_also_access_user_pages(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    create_and_login_admin(isolated_auth_client)

    response = isolated_auth_client.get(path, follow_redirects=False)

    assert response.status_code == 200


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_admin_pages_contain_post_logout_form(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    create_and_login_admin(isolated_auth_client)

    response = isolated_auth_client.get(path)

    assert 'method="post"' in response.text
    assert 'action="/logout"' in response.text


def test_disabled_admin_session_is_cleared_immediately(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
) -> None:
    admin = create_and_login_admin(isolated_auth_client)
    with sqlite3.connect(auth_db_path) as connection:
        connection.execute(
            "UPDATE users SET is_active = 0 WHERE id = ?",
            (admin["id"],),
        )

    response = isolated_auth_client.get("/admin/reviews", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert isolated_auth_client.cookies.get("bank_ocr_session") is None


def test_deleted_admin_session_is_cleared_immediately(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
) -> None:
    admin = create_and_login_admin(isolated_auth_client)
    with sqlite3.connect(auth_db_path) as connection:
        connection.execute("DELETE FROM users WHERE id = ?", (admin["id"],))

    response = isolated_auth_client.get("/admin/reviews", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert isolated_auth_client.cookies.get("bank_ocr_session") is None


def test_session_with_missing_user_id_is_cleared(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
) -> None:
    admin = create_and_login_admin(isolated_auth_client)
    with sqlite3.connect(auth_db_path) as connection:
        connection.execute(
            "UPDATE users SET id = ? WHERE id = ?",
            (admin["id"] + 1000, admin["id"]),
        )

    response = isolated_auth_client.get("/admin/reviews", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert isolated_auth_client.cookies.get("bank_ocr_session") is None


def test_admin_role_downgrade_takes_effect_in_existing_session(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
) -> None:
    admin = create_and_login_admin(isolated_auth_client)
    with sqlite3.connect(auth_db_path) as connection:
        connection.execute(
            "UPDATE users SET role = 'user' WHERE id = ?",
            (admin["id"],),
        )

    response = isolated_auth_client.get("/admin/reviews", follow_redirects=False)

    assert response.status_code == 403
    assert "无权访问" in response.text
    assert isolated_auth_client.cookies.get("bank_ocr_session") is not None


def test_user_role_promotion_takes_effect_in_existing_session(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
) -> None:
    user = create_and_login_user(isolated_auth_client)
    denied = isolated_auth_client.get("/admin/reviews", follow_redirects=False)
    assert denied.status_code == 403
    with sqlite3.connect(auth_db_path) as connection:
        connection.execute(
            "UPDATE users SET role = 'admin' WHERE id = ?",
            (user["id"],),
        )

    allowed = isolated_auth_client.get("/admin/reviews", follow_redirects=False)

    assert allowed.status_code == 200


def test_admin_logout_clears_session_and_protects_admin_pages(
    isolated_auth_client: TestClient,
) -> None:
    create_and_login_admin(isolated_auth_client)

    response = isolated_auth_client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    protected = isolated_auth_client.get(
        "/admin/reviews",
        follow_redirects=False,
    )
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"


def test_get_logout_is_not_an_exit_endpoint_for_admin(
    isolated_auth_client: TestClient,
) -> None:
    create_and_login_admin(isolated_auth_client)

    response = isolated_auth_client.get("/logout", follow_redirects=False)

    assert response.status_code == 405
    assert isolated_auth_client.get("/admin/reviews").status_code == 200


def test_review_record_read_apis_reject_anonymous_requests_with_json(
    isolated_auth_client: TestClient,
) -> None:
    list_response = isolated_auth_client.get(
        "/review-records",
        follow_redirects=False,
    )
    detail_response = isolated_auth_client.get(
        "/review-records/missing-request-id",
        follow_redirects=False,
    )

    assert list_response.status_code == 401
    assert detail_response.status_code == 401
    assert list_response.json() == {"detail": "Authentication required."}
    assert detail_response.json() == {"detail": "Authentication required."}
