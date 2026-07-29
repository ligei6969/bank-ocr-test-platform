"""Tests for the bank-card review API."""

import os
from pathlib import Path

import allure
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.field_parser import parse_bank_card_fields


client: TestClient
ARTIFACT_DIR = Path("reports") / "test-artifacts" / "api"
BANK_CARD_DATA_DIR = Path("data") / "processed" / "bank_card"


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


def create_dark_upload_image(path: Path) -> None:
    image = Image.new("RGB", (760, 460), (25, 25, 25))
    image.save(path)


def assert_ocr_and_parser_not_called(monkeypatch) -> None:
    def fail_recognize_text(image_path: str, mode: str = "mock") -> list[str]:
        pytest.fail("OCR should not be called for invalid uploads")

    def fail_parse_bank_card_fields(ocr_text: str) -> dict[str, str | None]:
        pytest.fail("Field parsing should not be called for invalid uploads")

    monkeypatch.setattr("app.main.recognize_text", fail_recognize_text)
    monkeypatch.setattr("app.main.parse_bank_card_fields", fail_parse_bank_card_fields)


@allure.description("银行卡正常审核：上传可读图片并识别完整字段时，接口返回 pass 和核心 JSON 字段。")
def test_bank_card_review_api_returns_review_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.recognize_text",
        lambda image_path, mode="mock": [
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
    assert isinstance(data, dict)
    assert "quality" in data
    assert "fields" in data
    assert "review_result" in data
    assert data["review_result"] == "pass"
    assert data["fields"]["card_number"] == "6222020202020001"
    assert data["fields"]["name"] == "ZHANG SAN"
    assert data["fields"]["valid_date"] == "12/30"
    assert data["quality"]["quality_result"] == "pass"
    assert data["quality"]["quality_reasons"] == []
    assert data["review_reasons"] == []


def test_root_returns_service_message() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"message": "Bank OCR test platform"}


def test_bank_card_review_ui_returns_html() -> None:
    response = client.get("/bank-card/ui")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="bank-card-review-app"' in response.text
    assert "/bank-card/review" in response.text or "bank_card_review.js" in response.text


def test_pytest_does_not_inherit_shell_ocr_mode() -> None:
    assert "OCR_MODE" not in os.environ


def test_bank_card_review_uses_mock_ocr_mode_by_default(monkeypatch) -> None:
    observed_modes: list[str] = []

    def fake_recognize_text(image_path: str, mode: str = "mock") -> list[str]:
        observed_modes.append(mode)
        return [
            "TEST BANK",
            "6222 0202 0202 0001",
            "CARD HOLDER",
            "ZHANG SAN",
            "VALID THRU 12/30",
        ]

    monkeypatch.delenv("OCR_MODE", raising=False)
    monkeypatch.setattr("app.main.recognize_text", fake_recognize_text)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "default_ocr_mode.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 200
    assert observed_modes == ["mock"]


def test_bank_card_review_uses_explicit_mock_ocr_mode(monkeypatch) -> None:
    observed_modes: list[str] = []

    def fake_recognize_text(image_path: str, mode: str = "mock") -> list[str]:
        observed_modes.append(mode)
        return [
            "TEST BANK",
            "6222 0202 0202 0001",
            "CARD HOLDER",
            "ZHANG SAN",
            "VALID THRU 12/30",
        ]

    monkeypatch.setenv("OCR_MODE", "mock")
    monkeypatch.setattr("app.main.recognize_text", fake_recognize_text)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "default_ocr_mode.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 200
    assert observed_modes == ["mock"]


def test_bank_card_review_uses_paddle_ocr_mode_from_environment(monkeypatch) -> None:
    observed_modes: list[str] = []

    def fake_recognize_text(image_path: str, mode: str = "mock") -> list[str]:
        observed_modes.append(mode)
        return [
            "TEST BANK",
            "6222 0202 0202 0001",
            "CARD HOLDER",
            "ZHANG SAN",
            "VALID THRU 12/30",
        ]

    monkeypatch.setenv("OCR_MODE", "paddle")
    monkeypatch.setattr("app.main.recognize_text", fake_recognize_text)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "paddle_ocr_mode.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 200
    assert observed_modes == ["paddle"]


def test_bank_card_review_does_not_accept_ocr_mode_from_request(monkeypatch) -> None:
    observed_modes: list[str] = []

    def fake_recognize_text(image_path: str, mode: str = "mock") -> list[str]:
        observed_modes.append(mode)
        return [
            "TEST BANK",
            "6222 0202 0202 0001",
            "CARD HOLDER",
            "ZHANG SAN",
            "VALID THRU 12/30",
        ]

    monkeypatch.delenv("OCR_MODE", raising=False)
    monkeypatch.setattr("app.main.recognize_text", fake_recognize_text)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "request_ocr_mode_ignored.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review?mode=paddle", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 200
    assert observed_modes == ["mock"]


def test_bank_card_review_rejects_invalid_ocr_mode(monkeypatch) -> None:
    def fail_recognize_text(image_path: str, mode: str = "mock") -> list[str]:
        pytest.fail("OCR should not be called for an invalid OCR_MODE")

    monkeypatch.setenv("OCR_MODE", "invalid")
    monkeypatch.setattr("app.main.recognize_text", fail_recognize_text)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "invalid_ocr_mode.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 500
    data = response.json()
    assert data["detail"] == "Invalid OCR_MODE. Use 'mock' or 'paddle'."
    assert isinstance(data["request_id"], str)
    assert len(data["request_id"]) == 32
    assert data["review_reasons"] == ["invalid_ocr_mode"]


def test_bank_card_review_requires_file_field() -> None:
    response = client.post("/bank-card/review", files={})

    assert response.status_code == 422


def test_bank_card_review_rejects_empty_file(monkeypatch) -> None:
    assert_ocr_and_parser_not_called(monkeypatch)

    response = client.post("/bank-card/review", files={"file": ("empty.png", b"", "image/png")})

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is empty."


def test_bank_card_review_rejects_text_file(monkeypatch) -> None:
    assert_ocr_and_parser_not_called(monkeypatch)

    response = client.post("/bank-card/review", files={"file": ("notes.txt", b"not an image", "text/plain")})

    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported file type. Upload a PNG or JPEG image."
    assert response.json()["review_reasons"] == ["invalid_file_type"]


@allure.description("图片质量读取异常：质量检测抛出解码错误时返回明确 400，且不进入 OCR。")
def test_bank_card_review_rejects_image_when_quality_reader_fails(monkeypatch) -> None:
    def raise_quality_error(image_path: str) -> dict:
        raise ValueError("image cannot be decoded")

    def fail_recognize_text(image_path: str, mode: str = "mock") -> list[str]:
        pytest.fail("OCR should not be called when quality checking fails")

    monkeypatch.setattr("app.main.check_image_quality", raise_quality_error)
    monkeypatch.setattr("app.main.recognize_text", fail_recognize_text)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "quality_reader_error.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is not a readable image."


@allure.description("非法卡号：字段完整但卡号格式非法时，审核结果为 reject。")
def test_bank_card_review_rejects_invalid_card_number(monkeypatch) -> None:
    monkeypatch.setattr("app.main.recognize_text", lambda image_path, mode="mock": ["OCR text with invalid card number"])
    monkeypatch.setattr(
        "app.main.parse_bank_card_fields",
        lambda ocr_text: {"card_number": "123", "valid_date": "12/30", "name": "ZHANG SAN"},
    )
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "invalid_card_number.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 200
    data = response.json()
    assert data["review_result"] == "reject"
    assert data["fields"]["card_number"] == "123"
    assert data["review_reasons"] == ["invalid_card_number"]


@allure.description("损坏图片拒绝处理：伪装成 PNG 的损坏内容返回 400，后续请求仍可用。")
def test_bank_card_review_rejects_corrupt_png_and_remains_available(monkeypatch) -> None:
    assert_ocr_and_parser_not_called(monkeypatch)

    response = client.post("/bank-card/review", files={"file": ("broken.png", b"not a real png", "image/png")})

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is not a readable image."

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
    monkeypatch.setattr("app.main.parse_bank_card_fields", parse_bank_card_fields)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "available_after_error.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        next_response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert next_response.status_code == 200


@allure.description("OCR 字段缺失：OCR 无文本时接口不崩溃，字段为空并进入 review。")
def test_bank_card_review_handles_empty_ocr_text(monkeypatch) -> None:
    monkeypatch.setattr("app.main.recognize_text", lambda image_path, mode="mock": [])
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "empty_ocr.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 200
    data = response.json()
    assert data["review_result"] == "review"
    assert data["fields"] == {
        "card_number": None,
        "valid_date": None,
        "name": None,
    }
    assert data["review_reasons"] == ["missing_card_number", "missing_valid_date", "missing_name"]


def test_bank_card_review_handles_partial_ocr_fields(monkeypatch) -> None:
    monkeypatch.setattr("app.main.recognize_text", lambda image_path, mode="mock": ["6222 0202 0202 0001"])
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "partial_ocr.png"
    create_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 200
    data = response.json()
    assert data["review_result"] == "review"
    assert data["fields"] == {
        "card_number": "6222020202020001",
        "valid_date": None,
        "name": None,
    }
    assert data["review_reasons"] == ["missing_valid_date", "missing_name"]


@allure.description("模糊图片人工复核：真实 blur 样本应被检测为模糊，并返回 review。")
def test_bank_card_review_returns_review_for_blurry_image(monkeypatch) -> None:
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
    image_path = BANK_CARD_DATA_DIR / "blur" / "bank_card_0001.png"

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 200
    data = response.json()
    assert data["review_result"] == "review"
    assert data["quality"]["is_blur"] is True
    assert "image_blur" in data["quality"]["quality_reasons"]
    assert "image_blur" in data["review_reasons"]


def test_bank_card_review_returns_review_for_bad_image_quality(monkeypatch) -> None:
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
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = ARTIFACT_DIR / "dark_bank_card.png"
    create_dark_upload_image(image_path)

    with image_path.open("rb") as file:
        response = client.post("/bank-card/review", files={"file": ("bank_card.png", file, "image/png")})

    assert response.status_code == 200
    data = response.json()
    assert data["review_result"] == "review"
    assert data["quality"]["quality_result"] == "review"
    assert "image_dark" in data["quality"]["quality_reasons"]
    assert "image_dark" in data["review_reasons"]
