"""Tests for Session-bound synchronizer CSRF protection."""

from __future__ import annotations

import base64
import io
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.review_records import save_review_record
from app.users import create_user


PASSWORD = "Csrf-test-password-123!"
CSRF_ERROR = {"detail": "CSRF validation failed."}
REVIEW_ENDPOINTS = ("/bank-card/review", "/id-card/review")


def get_csrf_token(client: TestClient) -> str:
    response = client.get("/csrf-token")
    assert response.status_code == 200
    token = response.json()["csrf_token"]
    assert isinstance(token, str)
    return token


def decode_session(client: TestClient) -> dict:
    cookie = client.cookies.get("bank_ocr_session")
    assert cookie is not None
    return json.loads(base64.b64decode(cookie.split(".", maxsplit=1)[0]))


def create_account(
    *,
    username: str,
    role: str = "user",
    is_active: bool = True,
) -> dict:
    return create_user(
        username=username,
        password=PASSWORD,
        role=role,
        is_active=is_active,
    )


def login_with_token(
    client: TestClient,
    *,
    username: str,
    password: str = PASSWORD,
    token: str,
):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )


def create_and_login(
    client: TestClient,
    *,
    username: str,
    role: str = "user",
) -> tuple[dict, str, str]:
    user = create_account(username=username, role=role)
    anonymous_token = get_csrf_token(client)
    response = login_with_token(
        client,
        username=username,
        token=anonymous_token,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/user"
    authenticated_token = get_csrf_token(client)
    return user, anonymous_token, authenticated_token


def image_upload() -> dict[str, tuple[str, io.BytesIO, str]]:
    image_bytes = io.BytesIO()
    Image.new("RGB", (320, 200), (120, 130, 140)).save(
        image_bytes,
        format="PNG",
    )
    image_bytes.seek(0)
    return {"file": ("synthetic.png", image_bytes, "image/png")}


def configure_successful_review(monkeypatch, endpoint: str) -> None:
    monkeypatch.setattr(
        "app.main.check_image_quality",
        lambda image_path: {
            "is_blur": False,
            "brightness": "normal",
            "has_glare": False,
            "quality_result": "pass",
        },
    )
    monkeypatch.setattr(
        "app.main.recognize_text",
        lambda image_path, mode="mock": ["SYNTHETIC OCR"],
    )
    if endpoint == "/bank-card/review":
        monkeypatch.setattr(
            "app.main.parse_bank_card_fields",
            lambda ocr_text: {
                "card_number": "6222020202020001",
                "valid_date": "12/30",
                "name": "ZHANG SAN",
            },
        )
    else:
        monkeypatch.setattr(
            "app.main.parse_id_card_fields",
            lambda ocr_text: {
                "side": "front",
                "fields": {
                    "name": "LI LEI",
                    "gender": "M",
                    "nation": "HAN",
                    "birth": "1986-01-22",
                    "address": "TEST ADDRESS",
                    "id_number": "110101198601220011",
                },
            },
        )


def review_record_count(database_path: Path) -> int:
    if not database_path.exists():
        return 0
    with sqlite3.connect(database_path) as connection:
        table = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'review_records'
            """
        ).fetchone()
        if table is None:
            return 0
        return connection.execute("SELECT COUNT(*) FROM review_records").fetchone()[0]


def fail_if_ocr_runs(image_path: str, mode: str = "mock") -> list[str]:
    pytest.fail("OCR must not run when CSRF validation fails.")


def test_anonymous_csrf_token_endpoint_returns_random_token_and_no_store(
    isolated_auth_client: TestClient,
) -> None:
    response = isolated_auth_client.get("/csrf-token")

    assert response.status_code == 200
    assert set(response.json()) == {"csrf_token"}
    token = response.json()["csrf_token"]
    assert isinstance(token, str)
    assert len(token) >= 32
    assert response.headers["cache-control"] == "no-store"
    assert "user_id" not in response.text
    assert "role" not in response.text
    assert "password" not in response.text


def test_same_session_reuses_csrf_token(
    isolated_auth_client: TestClient,
) -> None:
    first = get_csrf_token(isolated_auth_client)
    second = get_csrf_token(isolated_auth_client)

    assert first == second


def test_different_clients_receive_different_csrf_tokens(
    isolated_auth_client: TestClient,
) -> None:
    first = get_csrf_token(isolated_auth_client)
    with TestClient(app) as second_client:
        second = get_csrf_token(second_client)

    assert first != second


def test_csrf_token_is_not_logged(
    isolated_auth_client: TestClient,
    caplog,
) -> None:
    token = get_csrf_token(isolated_auth_client)

    assert token not in caplog.text


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 403),
        ({"X-CSRF-Token": "incorrect-token"}, 403),
    ],
)
def test_login_rejects_missing_or_wrong_csrf_token_with_json(
    isolated_auth_client: TestClient,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    create_account(username="csrf-login-user")
    get_csrf_token(isolated_auth_client)

    response = isolated_auth_client.post(
        "/login",
        data={"username": "csrf-login-user", "password": PASSWORD},
        headers=headers,
        follow_redirects=False,
    )

    assert response.status_code == expected_status
    assert response.json() == CSRF_ERROR
    assert "application/json" in response.headers["content-type"]
    assert "location" not in response.headers
    assert "<html" not in response.text.lower()


def test_other_clients_csrf_token_cannot_log_in_current_session(
    isolated_auth_client: TestClient,
) -> None:
    create_account(username="cross-session-login")
    get_csrf_token(isolated_auth_client)
    with TestClient(app) as other_client:
        other_token = get_csrf_token(other_client)

    response = login_with_token(
        isolated_auth_client,
        username="cross-session-login",
        token=other_token,
    )

    assert response.status_code == 403
    assert response.json() == CSRF_ERROR


def test_correct_csrf_token_logs_in_and_rotates_token(
    isolated_auth_client: TestClient,
) -> None:
    user, anonymous_token, authenticated_token = create_and_login(
        isolated_auth_client,
        username="rotated-login-user",
    )

    assert authenticated_token != anonymous_token
    session = decode_session(isolated_auth_client)
    assert session["user_id"] == user["id"]
    assert session["csrf_token"] == authenticated_token
    assert set(session) == {"user_id", "csrf_token"}


def test_failed_password_preserves_token_without_authentication(
    isolated_auth_client: TestClient,
) -> None:
    create_account(username="wrong-password-user")
    token = get_csrf_token(isolated_auth_client)

    response = login_with_token(
        isolated_auth_client,
        username="wrong-password-user",
        password="wrong-password",
        token=token,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=1"
    assert get_csrf_token(isolated_auth_client) == token
    assert decode_session(isolated_auth_client) == {"csrf_token": token}
    assert isolated_auth_client.get("/user", follow_redirects=False).status_code == 303


def test_csrf_failure_happens_before_password_verification(
    isolated_auth_client: TestClient,
    monkeypatch,
) -> None:
    create_account(username="no-password-check-user")
    get_csrf_token(isolated_auth_client)

    def fail_verify_password(password: str, password_hash: str) -> bool:
        pytest.fail("Password verification must not run after CSRF failure.")

    monkeypatch.setattr("app.auth_routes.verify_password", fail_verify_password)

    response = isolated_auth_client.post(
        "/login",
        data={"username": "no-password-check-user", "password": PASSWORD},
        headers={"X-CSRF-Token": "incorrect-token"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-CSRF-Token": "incorrect-token"},
    ],
)
def test_logout_rejects_invalid_csrf_and_keeps_user_logged_in(
    isolated_auth_client: TestClient,
    headers: dict[str, str],
) -> None:
    _user, _old_token, _current_token = create_and_login(
        isolated_auth_client,
        username=f"logout-invalid-{len(headers)}",
    )

    response = isolated_auth_client.post(
        "/logout",
        headers=headers,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.json() == CSRF_ERROR
    assert isolated_auth_client.get("/user").status_code == 200


def test_correct_csrf_token_logs_out_and_old_token_cannot_start_new_session(
    isolated_auth_client: TestClient,
) -> None:
    _user, _anonymous_token, authenticated_token = create_and_login(
        isolated_auth_client,
        username="csrf-logout-user",
    )

    response = isolated_auth_client.post(
        "/logout",
        headers={"X-CSRF-Token": authenticated_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert isolated_auth_client.cookies.get("bank_ocr_session") is None
    replay = login_with_token(
        isolated_auth_client,
        username="csrf-logout-user",
        token=authenticated_token,
    )
    assert replay.status_code == 403
    assert replay.json() == CSRF_ERROR


@pytest.mark.parametrize("endpoint", REVIEW_ENDPOINTS)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-CSRF-Token": "incorrect-token"},
    ],
)
def test_authenticated_review_rejects_invalid_csrf_before_ocr_and_audit(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
    monkeypatch,
    endpoint: str,
    headers: dict[str, str],
) -> None:
    create_and_login(
        isolated_auth_client,
        username=f"review-invalid-{endpoint.split('/')[1]}-{len(headers)}",
    )
    monkeypatch.setattr("app.main.recognize_text", fail_if_ocr_runs)

    response = isolated_auth_client.post(
        endpoint,
        files=image_upload(),
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json() == CSRF_ERROR
    assert review_record_count(auth_db_path) == 0


@pytest.mark.parametrize("endpoint", REVIEW_ENDPOINTS)
def test_anonymous_review_still_returns_401_before_csrf(
    isolated_auth_client: TestClient,
    endpoint: str,
) -> None:
    response = isolated_auth_client.post(
        endpoint,
        files=image_upload(),
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}


@pytest.mark.parametrize("endpoint", REVIEW_ENDPOINTS)
def test_correct_csrf_token_preserves_successful_review_contract(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
    monkeypatch,
    endpoint: str,
) -> None:
    _user, _anonymous_token, authenticated_token = create_and_login(
        isolated_auth_client,
        username=f"review-success-{endpoint.split('/')[1]}",
    )
    configure_successful_review(monkeypatch, endpoint)

    response = isolated_auth_client.post(
        endpoint,
        files=image_upload(),
        headers={"X-CSRF-Token": authenticated_token},
    )

    assert response.status_code == 200
    assert {
        "request_id",
        "review_result",
        "review_reasons",
        "quality",
        "ocr_text",
        "fields",
    } <= set(response.json())
    assert review_record_count(auth_db_path) == 1


def test_read_only_review_record_gets_do_not_require_csrf(
    isolated_auth_client: TestClient,
) -> None:
    create_and_login(
        isolated_auth_client,
        username="csrf-get-admin",
        role="admin",
    )
    save_review_record(
        request_id="csrf-get-record",
        doc_type="bank_card",
        filename="synthetic.png",
        ocr_mode="mock",
        review_result="pass",
        quality_result="pass",
        quality_reasons=[],
        review_reasons=[],
        fields={"synthetic": True},
    )

    list_response = isolated_auth_client.get("/review-records")
    detail_response = isolated_auth_client.get(
        "/review-records/csrf-get-record",
    )

    assert list_response.status_code == 200
    assert detail_response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/user",
        "/user/bank-card",
        "/user/id-card",
        "/admin/reviews",
        "/admin/reviews/test-request-id",
    ],
)
def test_authenticated_get_pages_do_not_require_csrf_header(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    create_and_login(
        isolated_auth_client,
        username=f"csrf-page-{path.count('/')}-{path.split('/')[-1]}",
        role="admin",
    )

    response = isolated_auth_client.get(path, follow_redirects=False)

    assert response.status_code == 200


def test_csrf_token_is_not_stored_in_database_schema(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
) -> None:
    _user, _anonymous_token, authenticated_token = create_and_login(
        isolated_auth_client,
        username="csrf-database-user",
    )

    with sqlite3.connect(auth_db_path) as connection:
        user_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        stored_user = connection.execute(
            "SELECT username, role FROM users WHERE username = ?",
            ("csrf-database-user",),
        ).fetchone()

    assert "csrf_token" not in user_columns
    assert stored_user == ("csrf-database-user", "user")
    assert authenticated_token not in json.dumps(stored_user)


def test_frontend_uses_header_memory_and_no_persistent_storage(
    isolated_auth_client: TestClient,
) -> None:
    security_script = isolated_auth_client.get(
        "/static/portal/portal_security.js",
    ).text
    login_script = isolated_auth_client.get("/static/portal/login.js").text
    bank_script = isolated_auth_client.get(
        "/static/portal/user_bank_card.js",
    ).text
    id_script = isolated_auth_client.get(
        "/static/portal/user_id_card.js",
    ).text
    combined = "\n".join(
        [security_script, login_script, bank_script, id_script]
    )

    assert 'headers.set("X-CSRF-Token", token)' in security_script
    assert 'fetch("/csrf-token"' in security_script
    assert "fetchWithCsrf" in login_script
    assert "fetchWithCsrf" in bank_script
    assert "fetchWithCsrf" in id_script
    assert "localStorage" not in combined
    assert "sessionStorage" not in combined
    assert "console.log" not in combined
    assert "csrf_token=" not in combined


def test_all_logout_pages_load_shared_csrf_script() -> None:
    portal_dir = Path("app") / "static" / "portal"
    logout_pages = [
        path
        for path in portal_dir.glob("*.html")
        if 'action="/logout"' in path.read_text(encoding="utf-8")
    ]

    assert logout_pages
    for page in logout_pages:
        html = page.read_text(encoding="utf-8")
        assert 'src="/static/portal/portal_security.js"' in html
