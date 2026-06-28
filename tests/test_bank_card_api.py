"""Tests for the bank-card review API."""

from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import app


client = TestClient(app)
ARTIFACT_DIR = Path("reports") / "test-artifacts" / "api"


def create_upload_image(path: Path) -> None:
    image = Image.new("RGB", (760, 460), (120, 130, 140))
    draw = ImageDraw.Draw(image)
    for y in range(0, 460, 20):
        for x in range(0, 760, 20):
            color = (60, 70, 80) if (x // 20 + y // 20) % 2 == 0 else (180, 190, 200)
            draw.rectangle((x, y, x + 19, y + 19), fill=color)
    image.save(path)


def test_bank_card_review_api_returns_review_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.recognize_text",
        lambda image_path: [
            "TEST BANK",
            "SYNTHETIC CARD",
            "6222 0202 0202 0001",
            "CARD HOLDER",
            "ZHANG SAN",
            "VALID THRU 12/30",
            "FOR TEST ONLY",
        ],
    )
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "bank_card.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 200
    data = response.json()
    assert data["review_result"] == "pass"
    assert data["fields"]["card_number"] == "6222020202020001"
    assert data["fields"]["name"] == "ZHANG SAN"
    assert data["fields"]["valid_date"] == "12/30"
    assert data["quality"]["quality_result"] == "pass"
