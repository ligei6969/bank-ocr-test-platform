"""Authentication and role authorization tests for review APIs."""

from __future__ import annotations

import base64
import io
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.review_records import save_review_record
from app.users import create_user


USER_PASSWORD = "Api-user-password-123!"
ADMIN_PASSWORD = "Api-admin-password-123!"
REVIEW_ENDPOINTS = ("/bank-card/review", "/id-card/review")
ADMIN_API_PATHS = ("/review-records", "/review-records/test-request-id")


def get_csrf_token(client: TestClient) -> str:
    response = client.get("/csrf-token")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def create_account_and_login(
    client: TestClient,
    *,
    username: str,
    role: str,
) -> dict:
    password = ADMIN_PASSWORD if role == "admin" else USER_PASSWORD
    user = create_user(
        username=username,
        password=password,
        role=role,
    )
    token = get_csrf_token(client)
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/user"
    client.headers["X-CSRF-Token"] = get_csrf_token(client)
    return user


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


def fail_if_ocr_runs(image_path: str, mode: str = "mock") -> list[str]:
    pytest.fail("OCR must not run before API authentication succeeds.")


def review_record_count(database_path: Path) -> int:
    if not database_path.exists():
        return 0
    with sqlite3.connect(database_path) as connection:
        table_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'review_records'
            """
        ).fetchone()
        if table_exists is None:
            return 0
        return connection.execute("SELECT COUNT(*) FROM review_records").fetchone()[0]


def add_review_record(
    *,
    request_id: str,
    doc_type: str = "bank_card",
    review_result: str = "pass",
) -> None:
    save_review_record(
        request_id=request_id,
        doc_type=doc_type,
        filename="synthetic.png",
        ocr_mode="mock",
        review_result=review_result,
        quality_result="pass",
        quality_reasons=[],
        review_reasons=[],
        fields={"synthetic": True},
    )


@pytest.mark.parametrize("endpoint", REVIEW_ENDPOINTS)
def test_anonymous_review_request_returns_json_401_before_ocr_or_audit(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
    monkeypatch,
    endpoint: str,
) -> None:
    monkeypatch.setattr("app.main.recognize_text", fail_if_ocr_runs)

    response = isolated_auth_client.post(
        endpoint,
        files=image_upload(),
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}
    assert "application/json" in response.headers["content-type"]
    assert "location" not in response.headers
    assert "<html" not in response.text.lower()
    assert review_record_count(auth_db_path) == 0


@pytest.mark.parametrize("endpoint", REVIEW_ENDPOINTS)
def test_anonymous_authentication_precedes_file_validation(
    isolated_auth_client: TestClient,
    endpoint: str,
) -> None:
    response = isolated_auth_client.post(
        endpoint,
        files={},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required."


@pytest.mark.parametrize("role", ["user", "admin"])
@pytest.mark.parametrize("endpoint", REVIEW_ENDPOINTS)
def test_active_user_and_admin_can_submit_reviews_with_unchanged_contract(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
    monkeypatch,
    role: str,
    endpoint: str,
) -> None:
    create_account_and_login(
        isolated_auth_client,
        username=f"{role}-{endpoint.split('/')[1]}",
        role=role,
    )
    configure_successful_review(monkeypatch, endpoint)

    response = isolated_auth_client.post(endpoint, files=image_upload())

    assert response.status_code == 200
    data = response.json()
    common_keys = {
        "request_id",
        "review_result",
        "review_reasons",
        "quality",
        "ocr_text",
        "fields",
    }
    assert common_keys <= set(data)
    assert "user_id" not in data
    if endpoint == "/id-card/review":
        assert "side" in data
    assert review_record_count(auth_db_path) == 1


@pytest.mark.parametrize("endpoint", REVIEW_ENDPOINTS)
@pytest.mark.parametrize("mutation", ["disable", "delete"])
def test_stale_user_session_is_rejected_before_review_processing(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
    monkeypatch,
    endpoint: str,
    mutation: str,
) -> None:
    user = create_account_and_login(
        isolated_auth_client,
        username=f"stale-{mutation}-{endpoint.split('/')[1]}",
        role="user",
    )
    with sqlite3.connect(auth_db_path) as connection:
        if mutation == "disable":
            connection.execute(
                "UPDATE users SET is_active = 0 WHERE id = ?",
                (user["id"],),
            )
        else:
            connection.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    monkeypatch.setattr("app.main.recognize_text", fail_if_ocr_runs)

    response = isolated_auth_client.post(endpoint, files=image_upload())

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required."
    assert isolated_auth_client.cookies.get("bank_ocr_session") is None
    assert review_record_count(auth_db_path) == 0


def test_anonymous_review_record_list_returns_json_401(
    isolated_auth_client: TestClient,
) -> None:
    response = isolated_auth_client.get(
        "/review-records",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}
    assert "application/json" in response.headers["content-type"]
    assert "location" not in response.headers


def test_normal_user_review_record_list_returns_json_403_without_data(
    isolated_auth_client: TestClient,
) -> None:
    create_account_and_login(
        isolated_auth_client,
        username="list-normal-user",
        role="user",
    )
    add_review_record(request_id="private-record")

    response = isolated_auth_client.get(
        "/review-records",
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Administrator access required.",
    }
    assert "private-record" not in response.text
    assert "location" not in response.headers


def test_admin_review_record_list_and_filters_keep_existing_contract(
    isolated_auth_client: TestClient,
) -> None:
    create_account_and_login(
        isolated_auth_client,
        username="list-admin",
        role="admin",
    )
    add_review_record(
        request_id="bank-review-record",
        doc_type="bank_card",
        review_result="review",
    )
    add_review_record(
        request_id="id-pass-record",
        doc_type="id_card",
        review_result="pass",
    )

    response = isolated_auth_client.get(
        "/review-records",
        params={"doc_type": "bank_card", "review_result": "review"},
    )

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert [record["request_id"] for record in response.json()] == [
        "bank-review-record"
    ]


def test_role_changes_apply_to_admin_api_in_existing_session(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
) -> None:
    user = create_account_and_login(
        isolated_auth_client,
        username="live-role-user",
        role="user",
    )
    denied = isolated_auth_client.get("/review-records")
    assert denied.status_code == 403
    with sqlite3.connect(auth_db_path) as connection:
        connection.execute(
            "UPDATE users SET role = 'admin' WHERE id = ?",
            (user["id"],),
        )
    promoted = isolated_auth_client.get("/review-records")
    assert promoted.status_code == 200
    with sqlite3.connect(auth_db_path) as connection:
        connection.execute(
            "UPDATE users SET role = 'user' WHERE id = ?",
            (user["id"],),
        )

    demoted = isolated_auth_client.get("/review-records")

    assert demoted.status_code == 403
    assert demoted.json()["detail"] == "Administrator access required."


@pytest.mark.parametrize(
    ("role", "expected_status", "expected_detail"),
    [
        (None, 401, "Authentication required."),
        ("user", 403, "Administrator access required."),
    ],
)
def test_non_admin_cannot_read_review_record_detail(
    isolated_auth_client: TestClient,
    role: str | None,
    expected_status: int,
    expected_detail: str,
) -> None:
    if role is not None:
        create_account_and_login(
            isolated_auth_client,
            username=f"detail-{role}",
            role=role,
        )
    add_review_record(request_id="protected-detail")

    response = isolated_auth_client.get(
        "/review-records/protected-detail",
        follow_redirects=False,
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert "protected-detail" not in response.text
    assert "location" not in response.headers


def test_admin_can_read_existing_detail_and_missing_detail_remains_404(
    isolated_auth_client: TestClient,
) -> None:
    create_account_and_login(
        isolated_auth_client,
        username="detail-admin",
        role="admin",
    )
    add_review_record(request_id="existing-detail")

    existing = isolated_auth_client.get("/review-records/existing-detail")
    missing = isolated_auth_client.get("/review-records/missing-detail")

    assert existing.status_code == 200
    assert existing.json()["request_id"] == "existing-detail"
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Review record not found."}


@pytest.mark.parametrize("mutation", ["disable", "delete"])
@pytest.mark.parametrize("path", ADMIN_API_PATHS)
def test_stale_admin_session_returns_401_for_admin_apis(
    isolated_auth_client: TestClient,
    auth_db_path: Path,
    mutation: str,
    path: str,
) -> None:
    admin = create_account_and_login(
        isolated_auth_client,
        username=f"stale-admin-{mutation}-{path.count('/')}",
        role="admin",
    )
    with sqlite3.connect(auth_db_path) as connection:
        if mutation == "disable":
            connection.execute(
                "UPDATE users SET is_active = 0 WHERE id = ?",
                (admin["id"],),
            )
        else:
            connection.execute("DELETE FROM users WHERE id = ?", (admin["id"],))

    response = isolated_auth_client.get(path, follow_redirects=False)

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}
    assert isolated_auth_client.cookies.get("bank_ocr_session") is None


def test_authenticated_api_session_still_contains_only_user_id(
    isolated_auth_client: TestClient,
) -> None:
    user = create_account_and_login(
        isolated_auth_client,
        username="minimal-api-session",
        role="admin",
    )

    cookie = isolated_auth_client.cookies.get("bank_ocr_session")
    assert cookie is not None
    session_data = json.loads(base64.b64decode(cookie.split(".", maxsplit=1)[0]))

    assert session_data["user_id"] == user["id"]
    assert isinstance(session_data["csrf_token"], str)
    assert set(session_data) == {"user_id", "csrf_token"}
    assert "role" not in session_data


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/user", 303),
        ("/admin/reviews", 303),
        ("/bank-card/ui", 200),
    ],
)
def test_page_level_access_behavior_remains_separate_from_api_errors(
    isolated_auth_client: TestClient,
    path: str,
    expected_status: int,
) -> None:
    response = isolated_auth_client.get(path, follow_redirects=False)

    assert response.status_code == expected_status
    if expected_status == 303:
        assert response.headers["location"] == "/login"


@pytest.mark.parametrize(
    ("script_path", "expected_status_handling"),
    [
        (
            "/static/portal/user_bank_card.js",
            'response.status === 401',
        ),
        (
            "/static/portal/user_id_card.js",
            'response.status === 401',
        ),
        (
            "/static/portal/admin_reviews.js",
            'response.status === 403',
        ),
        (
            "/static/portal/admin_review_detail.js",
            'response.status === 404',
        ),
    ],
)
def test_portal_scripts_handle_api_authorization_without_unsafe_storage(
    isolated_auth_client: TestClient,
    script_path: str,
    expected_status_handling: str,
) -> None:
    script = isolated_auth_client.get(script_path).text

    assert 'response.status === 401' in script
    assert expected_status_handling in script
    assert "登录状态已失效，请重新登录。" in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "innerHTML" not in script
