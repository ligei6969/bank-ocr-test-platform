"""SQLite persistence and password helpers for application users."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import bcrypt

from app.review_records import get_review_db_path


class UserAlreadyExistsError(ValueError):
    """Raised when an account already uses the requested username."""


def _connect() -> sqlite3.Connection:
    database_path = get_review_db_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_user_database() -> None:
    """Create the users table without changing existing database objects."""
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )


def hash_password(password: str) -> str:
    """Return a salted bcrypt hash for a plaintext password."""
    if not password:
        raise ValueError("Password must not be empty.")
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > 72:
        raise ValueError("Password must not exceed 72 UTF-8 bytes.")
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Return whether a plaintext password matches a bcrypt hash."""
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except (TypeError, ValueError):
        return False


def _deserialize_user(row: sqlite3.Row) -> dict[str, Any]:
    user = dict(row)
    user["is_active"] = bool(user["is_active"])
    return user


def get_user_by_username(username: str) -> dict[str, Any] | None:
    """Look up one user by its exact, normalized username."""
    normalized_username = username.strip()
    if not normalized_username:
        return None

    initialize_user_database()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE username = ?",
            (normalized_username,),
        ).fetchone()
    return _deserialize_user(row) if row else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    """Look up one user by its numeric primary key."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        return None

    initialize_user_database()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _deserialize_user(row) if row else None


def create_user(
    *,
    username: str,
    password: str,
    role: str = "user",
    is_active: bool = True,
) -> dict[str, Any]:
    """Create and return a user account whose password is stored only as a hash."""
    normalized_username = username.strip()
    normalized_role = role.strip()
    if not normalized_username:
        raise ValueError("Username must not be empty.")
    if not normalized_role:
        raise ValueError("Role must not be empty.")

    password_hash = hash_password(password)
    created_at = datetime.now(timezone.utc).isoformat()
    initialize_user_database()

    try:
        with _connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users (
                    username, password_hash, role, is_active, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_username,
                    password_hash,
                    normalized_role,
                    int(is_active),
                    created_at,
                ),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        if "users.username" in str(exc):
            raise UserAlreadyExistsError(
                f"Username already exists: {normalized_username}"
            ) from exc
        raise

    return {
        "id": user_id,
        "username": normalized_username,
        "password_hash": password_hash,
        "role": normalized_role,
        "is_active": bool(is_active),
        "created_at": created_at,
    }
