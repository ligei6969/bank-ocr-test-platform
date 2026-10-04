"""Regressions for readable but obstructed or tilted card uploads."""

from pathlib import Path

import pytest
from PIL import Image

from app.field_parser import parse_bank_card_fields
from app.ocr_service import MOCK_OCR_TEXT
from app.quality_check import check_image_quality
from app.rule_check import review_bank_card_with_reasons
from app.boundary import detect_boundary_case
from app.main import app
from app.review_records import get_review_record
from fastapi.testclient import TestClient


DATA_DIR = Path(__file__).resolve().parents[1] / "data/processed/bank_card"


@pytest.mark.parametrize(
    ("kind", "index", "reason"),
    [("occlusion", 1, "image_occluded"), ("occlusion", 2, "image_occluded"),
     ("rotate", 2, "image_rotated"), ("rotate", 3, "image_rotated")],
)
def test_readable_fields_do_not_hide_geometry_defects(kind, index, reason):
    quality = check_image_quality(str(DATA_DIR / kind / f"bank_card_{index:04d}.png"))
    fields = parse_bank_card_fields("\n".join(MOCK_OCR_TEXT))
    result, reasons = review_bank_card_with_reasons(fields, quality)
    assert result == "review"
    assert reason in reasons


@pytest.mark.parametrize("index", range(1, 101))
def test_normal_artwork_is_not_reported_as_obstruction_or_rotation(index):
    quality = check_image_quality(str(DATA_DIR / "normal" / f"bank_card_{index:04d}.png"))
    assert "image_rotated" not in quality["quality_reasons"]
    assert "image_occluded" not in quality["quality_reasons"]


@pytest.mark.parametrize("index", range(1, 101))
def test_current_solid_occlusion_dataset_cannot_auto_pass(index):
    quality = check_image_quality(str(DATA_DIR / "occlusion" / f"bank_card_{index:04d}.png"))
    fields = parse_bank_card_fields("\n".join(MOCK_OCR_TEXT))
    assert "image_occluded" in quality["quality_reasons"]
    assert review_bank_card_with_reasons(fields, quality)[0] != "pass"


def test_negligible_rotation_is_not_rejected_just_for_its_directory():
    quality = check_image_quality(str(DATA_DIR / "rotate/bank_card_0001.png"))
    assert "image_rotated" not in quality["quality_reasons"]


@pytest.mark.parametrize("kind", ["occlusion", "rotate"])
def test_api_detects_pixels_even_when_upload_filename_is_normal(authenticated_client, kind):
    path = DATA_DIR / kind / "bank_card_0002.png"
    response = authenticated_client.post(
        "/bank-card/review", files={"file": ("normal.png", path.read_bytes(), "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["review_result"] == "review"
    reason = "image_occluded" if kind == "occlusion" else "image_rotated"
    assert reason in response.json()["review_reasons"]


@pytest.mark.parametrize("angle", [-7, 7, 90])
def test_rotation_is_detected_from_a_new_image(tmp_path, angle):
    path = tmp_path / "arbitrary.png"
    with Image.open(DATA_DIR / "normal" / "bank_card_0002.png") as image:
        image.rotate(angle, expand=True, fillcolor=(20, 35, 52)).save(path)
    assert "image_rotated" in check_image_quality(str(path))["quality_reasons"]


@pytest.mark.parametrize("side", ["front", "back"])
@pytest.mark.parametrize("kind", ["occlusion", "rotate"])
def test_id_card_geometry_is_detected_too(side, kind):
    path = DATA_DIR.parent / "id_card" / side / kind / f"id_{side}_0002.jpg"
    reason = "image_occluded" if kind == "occlusion" else "image_rotated"
    assert reason in check_image_quality(str(path))["quality_reasons"]


@pytest.mark.parametrize("reason", ["image_rotated", "image_occluded"])
def test_text_only_ai_cannot_clear_geometry_even_with_other_boundary_flags(reason):
    quality = {"quality_metrics": {"brightness_mean": 64.0}}
    assert detect_boundary_case({}, quality, "review", [reason, "image_dark"]) == []


@pytest.mark.parametrize("endpoint", ["/bank-card/review", "/id-card/review"])
def test_predictor_failure_is_saved_for_request_id_lookup(authenticated_client, monkeypatch, endpoint):
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic predictor failure")
    monkeypatch.setattr("app.main.recognize_text", fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.cookies.update(authenticated_client.cookies)
        client.headers.update(authenticated_client.headers)
        response = client.post(endpoint, files={
            "file": ("normal.png", (DATA_DIR / "normal/bank_card_0002.png").read_bytes(), "image/png"),
        })
    assert response.status_code == 500
    record = get_review_record(response.json()["request_id"])
    assert record is not None
    assert record["filename"] == "normal.png"
    assert record["review_result"] == "error"
    assert record["error_message"] == "Synthetic predictor failure"
