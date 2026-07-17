"""SQLite persistence for document review audit records."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT_DIR / "reports" / "review_records.db"


def get_review_db_path() -> Path:
    configured_path = os.getenv("REVIEW_RECORDS_DB_PATH")
    return Path(configured_path) if configured_path else DEFAULT_DB_PATH


def _connect() -> sqlite3.Connection:
    database_path = get_review_db_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_review_database() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS review_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                doc_type TEXT NOT NULL,
                filename TEXT NOT NULL,
                ocr_mode TEXT,
                review_result TEXT NOT NULL,
                quality_result TEXT,
                quality_reasons TEXT NOT NULL,
                review_reasons TEXT NOT NULL DEFAULT '[]',
                fields_json TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(review_records)").fetchall()
        }
        if "review_reasons" not in columns:
            connection.execute(
                "ALTER TABLE review_records ADD COLUMN review_reasons TEXT NOT NULL DEFAULT '[]'"
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_review_records_filters
            ON review_records (doc_type, review_result, created_at)
            """
        )


def save_review_record(
    *,
    request_id: str,
    doc_type: str,
    filename: str,
    ocr_mode: str | None,
    review_result: str,
    quality_result: str | None,
    quality_reasons: list[str],
    fields: dict[str, Any],
    review_reasons: list[str] | None = None,
    error_message: str | None = None,
) -> None:
    initialize_review_database()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO review_records (
                request_id, doc_type, filename, ocr_mode, review_result,
                quality_result, quality_reasons, review_reasons, fields_json,
                error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                doc_type,
                filename,
                ocr_mode,
                review_result,
                quality_result,
                json.dumps(quality_reasons, ensure_ascii=False),
                json.dumps(review_reasons or [], ensure_ascii=False),
                json.dumps(fields, ensure_ascii=False),
                error_message,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def _deserialize_record(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["quality_reasons"] = json.loads(record["quality_reasons"])
    record["review_reasons"] = json.loads(record["review_reasons"])
    record["fields_json"] = json.loads(record["fields_json"])
    return record


def get_review_record(request_id: str) -> dict[str, Any] | None:
    initialize_review_database()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM review_records WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return _deserialize_record(row) if row else None


def list_review_records(
    *,
    doc_type: str | None = None,
    review_result: str | None = None,
) -> list[dict[str, Any]]:
    initialize_review_database()
    clauses: list[str] = []
    parameters: list[str] = []
    if doc_type:
        clauses.append("doc_type = ?")
        parameters.append(doc_type)
    if review_result:
        clauses.append("review_result = ?")
        parameters.append(review_result)

    query = "SELECT * FROM review_records"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC"

    with _connect() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [_deserialize_record(row) for row in rows]
