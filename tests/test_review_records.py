"""Tests for review audit records, request IDs, and log masking."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.logging_utils import mask_sensitive_data
from app.review_records import get_review_record as _get_review_record
from app.sqlite_connection import connect_database


client: TestClient


@pytest.fixture(autouse=True)
def use_authenticated_admin_client(
    authenticated_admin_client: TestClient,
) -> None:
    """Run existing audit-record assertions as an isolated administrator."""
    global client
    client = authenticated_admin_client


@pytest.fixture
def review_db(monkeypatch, tmp_path: Path) -> Path:
    database_path = tmp_path / "review_records.db"
    monkeypatch.setenv("REVIEW_RECORDS_DB_PATH", str(database_path))
    monkeypatch.setenv("OCR_MODE", "mock")
    return database_path


def create_upload_image(path: Path) -> None:
    image = Image.new("RGB", (760, 460), (120, 130, 140))
    draw = ImageDraw.Draw(image)
    for y in range(0, 460, 20):
        for x in range(0, 760, 20):
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

    with connect_database(review_db) as connection:
        row = connection.execute(
            "SELECT doc_type, filename, ocr_mode, review_result FROM review_records WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    # connect_database 把 row_factory 设为 sqlite3.Row，所以要和元组比较得先转
    assert tuple(row) == ("bank_card", "bank_card.png", "mock", "pass")


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

    with connect_database(review_db) as connection:
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
    # id_number 缺失且 mock OCR 文本里没有号码证据 → 附带归因码。
    # 见 app/ocr_evidence.py：这是「OCR 没认出来」，不是「解析器没取到」。
    assert id_reasons == ["missing_id_number", "evidence_missing_id_number"]
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

    with connect_database(review_db) as connection:
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
    with connect_database(review_db) as connection:
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

    with connect_database(review_db) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(review_records)").fetchall()
        }
    assert "review_reasons" in columns
    # 双判的 7 列也必须被增量补上 —— 这是升级路径，老库不能因为缺列而报错
    assert {
        "llm_invoked",
        "llm_override",
        "llm_decision",
        "llm_fallback_reason",
        "boundary_criteria",
        "llm_rationale",
        "quality_metrics",
    } <= columns


def test_dual_judge_columns_round_trip(review_db) -> None:
    """双判字段写入后能原样读回，且布尔/JSON 都被正确反序列化。"""
    from app.review_records import get_review_record, save_review_record

    save_review_record(
        request_id="dual-1",
        doc_type="bank_card",
        filename="a.png",
        ocr_mode="mock",
        review_result="pass",
        quality_result="review",
        quality_reasons=["glare_detected"],
        fields={"card_number": "6222020202020001"},
        review_reasons=["glare_detected"],
        llm_invoked=True,
        llm_override=True,
        llm_decision="pass",
        boundary_criteria=["false_positive_reason_code"],
        llm_rationale="反光位于镭射区",
        quality_metrics={"glare_component_ratio": 0.0066},
    )

    record = get_review_record("dual-1")

    assert record is not None
    assert record["llm_invoked"] is True
    assert record["llm_override"] is True
    assert record["llm_decision"] == "pass"
    assert record["boundary_criteria"] == ["false_positive_reason_code"]
    assert record["llm_rationale"] == "反光位于镭射区"
    assert record["quality_metrics"]["glare_component_ratio"] == 0.0066


def test_records_without_dual_judge_default_to_not_invoked(review_db) -> None:
    """没跑双判的记录：invoked=False 且 fallback_reason 为空。

    「没尝试复核」与「尝试了但失败」必须能区分 —— 否则改判率的分母
    会被故障样本稀释，而且方向是反的。
    """
    from app.review_records import get_review_record, save_review_record

    save_review_record(
        request_id="plain-1",
        doc_type="bank_card",
        filename="a.png",
        ocr_mode="mock",
        review_result="review",
        quality_result="review",
        quality_reasons=["image_blur"],
        fields={},
    )

    record = get_review_record("plain-1")

    assert record is not None
    assert record["llm_invoked"] is False
    assert record["llm_override"] is False
    assert record["llm_decision"] == ""
    assert record["llm_fallback_reason"] == ""
    assert record["boundary_criteria"] == []
    assert record["quality_metrics"] == {}


def test_default_review_database_is_gitignored() -> None:
    ignore_rules = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert "reports/review_records.db" in ignore_rules


def test_dual_judge_runs_inline_and_persists(review_db, monkeypatch, tmp_path) -> None:
    """端到端：边界样本经真实 HTTP 路由后，改判与判据都落进数据库。

    这是 P2.3 的核心验收点 —— 改判必须可审计，否则无法评估
    「LLM 到底帮了忙还是添了乱」。
    """
    monkeypatch.setattr(
        "app.main.check_image_quality",
        lambda image_path: {
            "is_blur": False,
            "brightness": "normal",
            "has_glare": True,
            "quality_result": "review",
            "quality_reasons": ["glare_detected"],
            "quality_metrics": {"glare_component_ratio": 0.0066},
            "severe_reasons": [],
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

    class _PassClient:
        enabled = True

        def adjudicate(self, payload):
            return {
                "decision": "pass",
                "overrode": True,
                "degraded": False,
                "reason": "",
                "rationale": "反光位于镭射区，关键字段已完整解析",
            }

    # 客户端在判定为边界样本后才取，所以补丁打在取客户端的地方
    monkeypatch.setattr("app.ai_client.get_ai_client", lambda: _PassClient())

    image_path = tmp_path / "bank_card.png"
    create_upload_image(image_path)
    with image_path.open("rb") as image_file:
        response = client.post(
            "/bank-card/review",
            files={"file": (image_path.name, image_file, "image/png")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["review_result"] == "pass"          # AI 改判生效
    assert body["dual_judge"]["llm_override"] is True
    assert body["dual_judge"]["boundary_criteria"] == ["false_positive_reason_code"]

    record = _get_review_record(body["request_id"])
    assert record is not None
    assert record["llm_invoked"] is True
    assert record["llm_override"] is True
    assert record["llm_decision"] == "pass"
    assert record["llm_rationale"].startswith("反光位于镭射区")
    assert record["quality_metrics"]["glare_component_ratio"] == 0.0066


def test_non_boundary_review_does_not_touch_the_ai_service(review_db, monkeypatch, tmp_path) -> None:
    """非边界样本：不调 AI，双判字段保持「未尝试」。—— 成本控制的核心。

    这里用「多个原因码」构造非边界样本：单一 glare_detected 是 C2 边界案例，
    会正常触发复核，拿它来测「不该调 AI」是测错了对象。
    """
    configure_bank_card_review(monkeypatch, quality_result="review")
    # 两个原因码 → C2 不成立（有交叉证据），且无 quality_metrics → C1 不成立
    # 替身要接受 ocr_text —— 真实函数现在带这个关键字参数（用于字段缺失归因），
    # 替身签名落后会让测试报 TypeError 而不是测它本来要测的东西。
    monkeypatch.setattr(
        "app.main.review_bank_card_with_reasons",
        lambda fields, quality, **kwargs: ("review", ["image_blur", "glare_detected"]),
    )

    def _explode():
        raise AssertionError("非边界样本不该调用 AI 服务")

    monkeypatch.setattr("app.ai_client.get_ai_client", _explode)

    response = post_bank_card(monkeypatch, tmp_path, quality_result="review")

    assert response.status_code == 200
    body = response.json()
    assert body["review_result"] == "review"
    assert body["dual_judge"]["llm_invoked"] is False
    assert body["dual_judge"]["boundary_criteria"] == []
