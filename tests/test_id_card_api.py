"""Tests for the ID-card review API."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import review_id_card


client: TestClient
ARTIFACT_DIR = Path("reports") / "test-artifacts" / "api"


@pytest.fixture(autouse=True)
def use_authenticated_user_client(
    authenticated_client: TestClient,
) -> None:
    """Run existing review assertions as an isolated active user."""
    global client
    client = authenticated_client


def create_upload_image(path: Path) -> None:
    image = Image.new("RGB", (760, 460), (120, 130, 140))
    draw = ImageDraw.Draw(image)
    for y in range(0, 460, 20):
        for x in range(0, 760, 20):
            color = (60, 70, 80) if (x // 20 + y // 20) % 2 == 0 else (180, 190, 200)
            draw.rectangle((x, y, x + 19, y + 19), fill=color)
    image.save(path)


def test_id_card_review_api_returns_front_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.recognize_text",
        lambda image_path, mode="mock": [
            "姓名 李雷",
            "性别 男 民族 苗",
            "出生 1986年1月22日",
            "住址 安徽省月江市城东区文昌街64号",
            "公民身份号码 110101198601220011",
        ],
    )
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "id_card_front.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/id-card/review", files={"file": ("id_card_front.png", file, "image/png")})

    assert response.status_code == 200
    data = response.json()
    assert data["review_result"] == "pass"
    assert data["side"] == "front"
    assert data["fields"]["name"] == "李雷"
    assert data["fields"]["id_number"] == "110101198601220011"


def test_id_card_review_api_returns_back_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.recognize_text",
        lambda image_path, mode="mock": [
            "中华人民共和国",
            "居民身份证",
            "签发机关 月江市公安局",
            "有效期限 2020.01.01-2040.01.01",
        ],
    )
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "id_card_back.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/id-card/review", files={"file": ("id_card_back.png", file, "image/png")})

    assert response.status_code == 200
    data = response.json()
    assert data["review_result"] == "pass"
    assert data["side"] == "back"
    assert data["fields"]["issue_authority"] == "月江市公安局"
    assert data["fields"]["valid_period"] == "2020.01.01-2040.01.01"


def test_id_card_review_requires_known_side() -> None:
    result = review_id_card(
        "unknown",
        {"name": "ZHANG SAN", "gender": "M", "nation": "HAN", "birth": "1990", "address": "ADDR", "id_number": "1"},
        {"quality_result": "pass"},
    )

    assert result == "review"


def test_id_card_review_requires_pass_quality() -> None:
    result = review_id_card(
        "back",
        {"issue_authority": "AUTHORITY", "valid_period": "2020-2040"},
        {"quality_result": "review"},
    )

    assert result == "review"


def test_id_card_review_requires_all_fields() -> None:
    result = review_id_card(
        "front",
        {"name": "ZHANG SAN", "gender": "M", "nation": "HAN", "birth": "1990", "address": "ADDR"},
        {"quality_result": "pass"},
    )

    assert result == "review"


def test_id_card_unknown_side_returns_reason(monkeypatch) -> None:
    monkeypatch.setattr("app.main.recognize_text", lambda image_path, mode="mock": ["UNRELATED OCR TEXT"])
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "id_card_front.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/id-card/review", files={"file": ("id_card_front.png", file, "image/png")})

    assert response.status_code == 200
    data = response.json()
    assert data["review_result"] == "review"
    assert data["side"] == "unknown"
    assert data["review_reasons"] == ["unknown_id_card_side"]
