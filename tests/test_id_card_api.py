"""Tests for the ID-card review API."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import ID_CARD_MAX_UPLOAD_BYTES, review_id_card


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


def test_id_card_upload_rejects_file_over_10_mib(monkeypatch) -> None:
    monkeypatch.setattr("app.main.recognize_text", lambda *args, **kwargs: pytest.fail("OCR must not run"))
    before = set(Path("reports/tmp_uploads").glob("*"))
    payload = b"x" * (ID_CARD_MAX_UPLOAD_BYTES + 1)

    response = client.post(
        "/id-card/review",
        files={"file": ("large.png", payload, "image/png")},
    )

    assert response.status_code == 413
    assert response.json()["review_reasons"] == ["file_too_large"]
    assert set(Path("reports/tmp_uploads").glob("*")) == before


def test_id_card_upload_rejects_mismatched_real_format(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.main.recognize_text", lambda *args, **kwargs: pytest.fail("OCR must not run"))
    image_path = tmp_path / "image.png"
    Image.new("RGB", (760, 460), "white").save(image_path, format="JPEG")

    with image_path.open("rb") as file:
        response = client.post(
            "/id-card/review",
            files={"file": ("image.png", file, "image/png")},
        )

    assert response.status_code == 400
    assert response.json()["review_reasons"] == ["invalid_image_format"]


@pytest.mark.parametrize(
    "size, reason",
    [
        ((299, 760), "image_dimensions_out_of_range"),
        ((8001, 500), "image_dimensions_out_of_range"),
        ((6000, 5000), "image_dimensions_out_of_range"),
        ((5000, 900), "image_aspect_ratio_invalid"),
    ],
)
def test_id_card_upload_rejects_unsafe_dimensions(monkeypatch, tmp_path: Path, size, reason) -> None:
    monkeypatch.setattr("app.main.recognize_text", lambda *args, **kwargs: pytest.fail("OCR must not run"))
    image_path = tmp_path / "image.png"
    Image.new("RGB", size, "white").save(image_path, format="PNG")

    with image_path.open("rb") as file:
        response = client.post(
            "/id-card/review",
            files={"file": ("image.png", file, "image/png")},
        )

    assert response.status_code == 400
    assert response.json()["review_reasons"] == [reason]


def test_id_card_upload_accepts_rotated_exif_dimensions(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.main.check_image_quality", lambda path: {"quality_result": "pass"})
    monkeypatch.setattr("app.main.recognize_text", lambda *args, **kwargs: ["中华人民共和国", "居民身份证"])
    image_path = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (500, 760), "white")
    exif = image.getexif()
    exif[274] = 6
    image.save(image_path, format="JPEG", exif=exif.tobytes())

    with image_path.open("rb") as file:
        response = client.post(
            "/id-card/review",
            files={"file": ("rotated.jpg", file, "image/jpeg")},
        )

    assert response.status_code == 200


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
