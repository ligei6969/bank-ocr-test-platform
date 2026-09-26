"""SQLite persistence for document review audit records."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.sqlite_connection import connect_database


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT_DIR / "reports" / "review_records.db"


def get_review_db_path() -> Path:
    configured_path = os.getenv("REVIEW_RECORDS_DB_PATH")
    return Path(configured_path) if configured_path else DEFAULT_DB_PATH


def initialize_review_database() -> None:
    with connect_database(get_review_db_path()) as connection:
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
                created_at TEXT NOT NULL,
                llm_invoked INTEGER NOT NULL DEFAULT 0,
                llm_override INTEGER NOT NULL DEFAULT 0,
                llm_decision TEXT NOT NULL DEFAULT '',
                llm_fallback_reason TEXT NOT NULL DEFAULT '',
                boundary_criteria TEXT NOT NULL DEFAULT '[]',
                llm_rationale TEXT NOT NULL DEFAULT '',
                quality_metrics TEXT NOT NULL DEFAULT '{}'
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
        # 双判（P2.3）落库。逐列判存在再 ALTER，与上面的 review_reasons 同一写法。
        #
        # 为什么 llm_invoked 与 llm_override 要分开：'未尝试复核' 与
        # '尝试了但失败' 在运维上完全不同 —— 前者是没走到边界判据，后者是
        # AI 服务出问题了。只用一个 bool 会让改判率的分母被故障样本稀释，
        # 而且故障越多久改判率越低，看起来像规则阈值的问题，方向是反的。
        for column, ddl in (
            ("llm_invoked", "INTEGER NOT NULL DEFAULT 0"),
            ("llm_override", "INTEGER NOT NULL DEFAULT 0"),
            ("llm_decision", "TEXT NOT NULL DEFAULT ''"),
            ("llm_fallback_reason", "TEXT NOT NULL DEFAULT ''"),
            ("boundary_criteria", "TEXT NOT NULL DEFAULT '[]'"),
            ("llm_rationale", "TEXT NOT NULL DEFAULT ''"),
            ("quality_metrics", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE review_records ADD COLUMN {column} {ddl}"
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
    llm_invoked: bool = False,
    llm_override: bool = False,
    llm_decision: str | None = None,
    llm_fallback_reason: str | None = None,
    boundary_criteria: list[str] | None = None,
    llm_rationale: str | None = None,
    quality_metrics: dict[str, Any] | None = None,
) -> None:
    initialize_review_database()
    with connect_database(get_review_db_path()) as connection:
        connection.execute(
            """
            INSERT INTO review_records (
                request_id, doc_type, filename, ocr_mode, review_result,
                quality_result, quality_reasons, review_reasons, fields_json,
                error_message, created_at,
                llm_invoked, llm_override, llm_decision, llm_fallback_reason,
                boundary_criteria, llm_rationale, quality_metrics
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                1 if llm_invoked else 0,
                1 if llm_override else 0,
                llm_decision or "",
                llm_fallback_reason or "",
                json.dumps(boundary_criteria or [], ensure_ascii=False),
                llm_rationale or "",
                json.dumps(quality_metrics or {}, ensure_ascii=False),
            ),
        )


def _deserialize_record(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["quality_reasons"] = json.loads(record["quality_reasons"])
    record["review_reasons"] = json.loads(record["review_reasons"])
    record["fields_json"] = json.loads(record["fields_json"])
    record["boundary_criteria"] = json.loads(record["boundary_criteria"])
    record["quality_metrics"] = json.loads(record["quality_metrics"])
    record["llm_invoked"] = bool(record["llm_invoked"])
    record["llm_override"] = bool(record["llm_override"])
    return record


def get_review_record(request_id: str) -> dict[str, Any] | None:
    initialize_review_database()
    with connect_database(get_review_db_path()) as connection:
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

    with connect_database(get_review_db_path()) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [_deserialize_record(row) for row in rows]
