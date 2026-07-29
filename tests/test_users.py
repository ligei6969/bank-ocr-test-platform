"""Tests for user persistence, password hashing, and account creation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.review_records import save_review_record
from app.users import (
    UserAlreadyExistsError,
    create_user,
    get_user_by_username,
    hash_password,
    initialize_user_database,
    verify_password,
)
from scripts import create_user as create_user_script


@pytest.fixture
def user_db(monkeypatch, tmp_path: Path) -> Path:
    database_path = tmp_path / "review_records.db"
    monkeypatch.setenv("REVIEW_RECORDS_DB_PATH", str(database_path))
    return database_path


def test_initialize_user_database_creates_required_schema(user_db: Path) -> None:
    initialize_user_database()

    with sqlite3.connect(user_db) as connection:
        columns = connection.execute("PRAGMA table_info(users)").fetchall()

    assert [column[1] for column in columns] == [
        "id",
        "username",
        "password_hash",
        "role",
        "is_active",
        "created_at",
    ]
    assert columns[3][4] == "'user'"
    assert columns[4][4] == "1"


def test_password_is_hashed_and_can_be_verified() -> None:
    password = "Strong-test-password-123!"

    password_hash = hash_password(password)

    assert password_hash != password
    assert password not in password_hash
    assert password_hash.startswith(("$2a$", "$2b$"))
    assert verify_password(password, password_hash) is True
    assert verify_password("wrong-password", password_hash) is False
    assert verify_password(password, "not-a-valid-hash") is False


def test_create_user_persists_hash_role_and_status(user_db: Path) -> None:
    user = create_user(
        username="  reviewer  ",
        password="Review-password-123!",
        role="admin",
        is_active=False,
    )

    with sqlite3.connect(user_db) as connection:
        stored = connection.execute(
            """
            SELECT username, password_hash, role, is_active, created_at
            FROM users WHERE username = ?
            """,
            ("reviewer",),
        ).fetchone()

    assert stored is not None
    assert stored[0] == "reviewer"
    assert stored[1] == user["password_hash"]
    assert stored[1] != "Review-password-123!"
    assert verify_password("Review-password-123!", stored[1])
    assert stored[2:4] == ("admin", 0)
    assert stored[4] == user["created_at"]


def test_get_user_by_username_returns_user_and_handles_missing(user_db: Path) -> None:
    created = create_user(username="alice", password="Alice-password-123!")

    found = get_user_by_username(" alice ")

    assert found == created
    assert found["role"] == "user"
    assert found["is_active"] is True
    assert get_user_by_username("missing") is None


def test_duplicate_username_is_rejected(user_db: Path) -> None:
    create_user(username="duplicate", password="First-password-123!")

    with pytest.raises(UserAlreadyExistsError):
        create_user(username="duplicate", password="Second-password-123!")


def test_users_and_review_records_coexist_in_same_database(user_db: Path) -> None:
    save_review_record(
        request_id="request-1",
        doc_type="bank_card",
        filename="synthetic.png",
        ocr_mode="mock",
        review_result="pass",
        quality_result="pass",
        quality_reasons=[],
        fields={"card_number": "622202******0001"},
    )

    create_user(username="auditor", password="Audit-password-123!")

    with sqlite3.connect(user_db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        review_count = connection.execute(
            "SELECT COUNT(*) FROM review_records"
        ).fetchone()[0]
        user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    assert {"review_records", "users"} <= tables
    assert review_count == 1
    assert user_count == 1


def test_create_user_script_creates_inactive_account(
    user_db: Path,
    monkeypatch,
    capsys,
) -> None:
    passwords = iter(["Script-password-123!", "Script-password-123!"])
    monkeypatch.setattr(create_user_script, "getpass", lambda prompt: next(passwords))

    exit_code = create_user_script.main(
        ["--username", "script-admin", "--role", "admin", "--inactive"]
    )

    assert exit_code == 0
    assert "Created user 'script-admin'" in capsys.readouterr().out
    created = get_user_by_username("script-admin")
    assert created is not None
    assert created["role"] == "admin"
    assert created["is_active"] is False
