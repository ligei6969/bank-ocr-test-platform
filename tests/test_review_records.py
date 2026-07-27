"""Tests for review audit records, request IDs, and log masking."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.logging_utils import mask_sensitive_data
from app.main import app


client = TestClient(app)


@pytest.fixture
def review_db(monkeypatch, tmp_path: Path) -> Path:
    database_path = tmp_path / "review_records.db"
    monkeypatch.setenv("REVIEW_RECORDS_DB_PATH", str(database_path))
    monkeypatch.setenv("OCR_MODE", "mock")
    return database_path


def create_upload_image(path: Path) -> None:
    image = Image.new("RGB", (320, 200), (120, 130, 140))
    draw = ImageDraw.Draw(image)
    for y in range(0, 200, 20):
        for x in range(0, 320, 20):
            color = (60, 70, 80) if (x // 20 + y // 20) % 2 == 0 else (180, 190, 200)
            draw.rectangle((x, y, x + 19, y + 19), fill=color)
    image.save(path)


def configure_bank_card_review(monkeypatch, *, quality_result: str = "pass") -> None:
    monkeypatch.setattr(
        "app.main.check_image_quality",
        lambda image_path: {
            "is_blur": False,
            "brightness": "normal",
            "has_glare": quality_result == "review",
            "quality_result": quality_result,
        },
    )
    monkeypatch.setattr(
        "app.main.recognize_text",
        lambda image_path, mode="mock": [
            "TEST BANK",
            "6222 0202 0202 0001",
            "CARD HOLDER",
            "ZHANG SAN",
            "VALID THRU 12/30",
        ],
    )


def post_bank_card(monkeypatch, tmp_path: Path, *, quality_result: str = "pass"):
    configure_bank_card_review(monkeypatch, quality_result=quality_result)
    image_path = tmp_path / "bank_card.png"
    create_upload_image(image_path)
    with image_path.open("rb") as image_file:
        return client.post(
            "/bank-card/review",
            files={"file": (image_path.name, image_file, "image/png")},
        )


def configure_id_card_review(monkeypatch, *, fields: dict[str, str | None]) -> None:
    monkeypatch.setattr(
        "app.main.check_image_quality",
        lambda image_path: {
            "is_blur": False,
            "brightness": "normal",
            "has_glare": False,
            "quality_result": "pass",
        },
    )
    monkeypatch.setattr("app.main.recognize_text", lambda image_path, mode="mock": ["ID CARD"])
    monkeypatch.setattr(
        "app.main.parse_id_card_fields",
        lambda ocr_text: {
            "side": "front",
            "fields": fields,
        },
    )


def post_id_card(monkeypatch, tmp_path: Path, *, fields: dict[str, str | None]):
    configure_id_card_review(monkeypatch, fields=fields)
    image_path = tmp_path / "id_card.png"
    create_upload_image(image_path)
    with image_path.open("rb") as image_file:
        return client.post(
            "/id-card/review",
            files={"file": (image_path.name, image_file, "image/png")},
        )


def test_bank_card_review_returns_request_id_and_writes_record(review_db, monkeypatch, tmp_path) -> None:
    response = post_bank_card(monkeypatch, tmp_path)

    assert response.status_code == 200
    request_id = response.json()["request_id"]
    assert len(request_id) == 32
    assert response.headers["X-Request-ID"] == request_id
    assert review_db.exists()

    with sqlite3.connect(review_db) as connection:
        row = connection.execute(
            "SELECT doc_type, filename, ocr_mode, review_result FROM review_records WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    assert row == ("bank_card", "bank_card.png", "mock", "pass")


def test_id_card_review_returns_request_id_and_can_be_queried(review_db, monkeypatch, tmp_path) -> None:
    response = post_id_card(
        monkeypatch,
        tmp_path,
        fields={
            "name": "LI LEI",
            "gender": "M",
            "nation": "HAN",
            "birth": "1986-01-22",
            "address": "TEST ADDRESS",
            "id_number": "110101198601220011",
        },
    )

    assert response.status_code == 200
    request_id = response.json()["request_id"]
    query_response = client.get(f"/review-records/{request_id}")
    assert query_response.status_code == 200
    record = query_response.json()
    assert record["request_id"] == request_id
    assert record["doc_type"] == "id_card"
    assert record["fields_json"]["id_number"] == "110101198601220011"
    assert record["review_reasons"] == []


def test_bank_card_and_id_card_records_share_table_but_keep_distinct_payloads(
    review_db,
    monkeypatch,
    tmp_path,
) -> None:
    bank_response = post_bank_card(monkeypatch, tmp_path)
    id_response = post_id_card(
        monkeypatch,
        tmp_path,
        fields={
            "name": "LI LEI",
            "gender": "M",
            "nation": "HAN",
            "birth": "1986-01-22",
            "address": "TEST ADDRESS",
            "id_number": None,
        },
    )

    assert bank_response.status_code == 200
    assert id_response.status_code == 200
    bank_request_id = bank_response.json()["request_id"]
    id_request_id = id_response.json()["request_id"]

    with sqlite3.connect(review_db) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'review_records'"
            ).fetchall()
        }
        rows = connection.execute(
            """
            SELECT request_id, doc_type, review_result, review_reasons, fields_json
            FROM review_records
            WHERE request_id IN (?, ?)
            """,
            (bank_request_id, id_request_id),
        ).fetchall()

    assert table_names == {"review_records"}
    records = {row[0]: row for row in rows}
    assert set(records) == {bank_request_id, id_request_id}

    bank_record = records[bank_request_id]
    bank_reasons = json.loads(bank_record[3])
    bank_fields = json.loads(bank_record[4])
    assert bank_record[1:3] == ("bank_card", "pass")
    assert bank_reasons == []
    assert set(bank_fields) == {"card_number", "valid_date", "name"}
    assert "id_number" not in bank_fields

    id_record = records[id_request_id]
    id_reasons = json.loads(id_record[3])
    id_fields = json.loads(id_record[4])
    assert id_record[1:3] == ("id_card", "review")
    assert id_reasons == ["missing_id_number"]
    assert "id_number" in id_fields
    assert "card_number" not in id_fields


def test_review_records_can_be_filtered_by_result(review_db, monkeypatch, tmp_path) -> None:
    response = post_bank_card(monkeypatch, tmp_path, quality_result="review")
    request_id = response.json()["request_id"]

    query_response = client.get(
        "/review-records",
        params={"doc_type": "bank_card", "review_result": "review"},
    )

    assert query_response.status_code == 200
    records = query_response.json()
    assert [record["request_id"] for record in records] == [request_id]
    assert records[0]["quality_reasons"] == ["glare_detected"]
    assert records[0]["review_reasons"] == ["glare_detected"]


def test_invalid_image_error_has_request_id_and_audit_record(review_db) -> None:
    response = client.post(
        "/bank-card/review",
        files={"file": ("broken.png", b"not a real image", "image/png")},
    )

    assert response.status_code == 400
    request_id = response.json()["request_id"]
    assert response.headers["X-Request-ID"] == request_id
    record = client.get(f"/review-records/{request_id}").json()
    assert record["review_result"] == "error"
    assert record["error_message"] == "Uploaded file is not a readable image."
    assert record["review_reasons"] == ["unreadable_image"]


def test_mask_sensitive_data_hides_bank_card_number() -> None:
    original = "card_number=6222020202020001"
    masked = mask_sensitive_data(original)

    assert "6222020202020001" not in masked
    assert masked == "card_number=622202******0001"


def test_mask_sensitive_data_hides_formatted_bank_card_number() -> None:
    masked = mask_sensitive_data("card=6222 0202 0202 0001")

    assert masked == "card=622202******0001"


def test_mask_sensitive_data_hides_id_card_number() -> None:
    original = "id_number=110101198601220011"
    masked = mask_sensitive_data(original)

    assert "110101198601220011" not in masked
    assert masked == "id_number=110101********0011"


def test_review_logs_do_not_include_full_bank_card_number(review_db, monkeypatch, tmp_path, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="app.main"):
        response = post_bank_card(monkeypatch, tmp_path)

    assert response.status_code == 200
    assert "6222020202020001" not in caplog.text
    assert "622202******0001" in caplog.text


def test_review_record_schema_contains_required_columns(review_db, monkeypatch, tmp_path) -> None:
    post_bank_card(monkeypatch, tmp_path)

    with sqlite3.connect(review_db) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(review_records)").fetchall()
        }
    assert {
        "id",
        "request_id",
        "doc_type",
        "filename",
        "ocr_mode",
        "review_result",
        "quality_result",
        "quality_reasons",
        "review_reasons",
        "fields_json",
        "error_message",
        "created_at",
    } <= columns


def test_existing_review_database_is_migrated(review_db) -> None:
    with sqlite3.connect(review_db) as connection:
        connection.execute(
            """
            CREATE TABLE review_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                doc_type TEXT NOT NULL,
                filename TEXT NOT NULL,
                ocr_mode TEXT,
                review_result TEXT NOT NULL,
                quality_result TEXT,
                quality_reasons TEXT NOT NULL,
                fields_json TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

    from app.review_records import initialize_review_database

    initialize_review_database()

    with sqlite3.connect(review_db) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(review_records)").fetchall()
        }
    assert "review_reasons" in columns


def test_default_review_database_is_gitignored() -> None:
    ignore_rules = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert "reports/review_records.db" in ignore_rules
