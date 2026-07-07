"""Application entry point for the bank OCR test platform."""

from __future__ import annotations

import os
import shutil
from uuid import uuid4
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from app.field_parser import parse_bank_card_fields
from app.id_card_parser import parse_id_card_fields
from app.ocr_service import recognize_text
from app.quality_check import check_image_quality
from app.rule_check import review_bank_card


app = FastAPI(title="Bank OCR Test Platform")
ROOT_DIR = Path(__file__).resolve().parents[1]
UPLOAD_DIR = ROOT_DIR / "reports" / "tmp_uploads"
STATIC_DIR = ROOT_DIR / "app" / "static"
ALLOWED_BANK_CARD_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
ALLOWED_OCR_MODES = {"mock", "paddle"}
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Bank OCR test platform"}


@app.get("/bank-card/ui", response_class=HTMLResponse)
def bank_card_review_ui() -> HTMLResponse:
    html = (STATIC_DIR / "bank_card_review.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


def get_ocr_mode() -> str:
    mode = os.getenv("OCR_MODE", "mock").strip().lower()
    if mode not in ALLOWED_OCR_MODES:
        raise HTTPException(status_code=500, detail="Invalid OCR_MODE. Use 'mock' or 'paddle'.")
    return mode


def save_upload_file(file: UploadFile) -> Path:
    suffix = Path(file.filename or "upload.png").suffix or ".png"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    image_path = UPLOAD_DIR / f"{uuid4().hex}{suffix}"
    with image_path.open("wb") as temp_file:
        shutil.copyfileobj(file.file, temp_file)
    return image_path


def validate_bank_card_upload(file: UploadFile) -> None:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_BANK_CARD_IMAGE_SUFFIXES:
        raise HTTPException(status_code=400, detail="Unsupported file type. Upload a PNG or JPEG image.")


def validate_readable_image(image_path: Path) -> None:
    if image_path.stat().st_size == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    try:
        with Image.open(image_path) as image:
            image.verify()
    except (OSError, UnidentifiedImageError) as exc:
        raise HTTPException(status_code=400, detail="Uploaded file is not a readable image.") from exc


@app.post("/bank-card/review")
def review_bank_card_image(file: UploadFile = File(...)) -> dict:
    ocr_mode = get_ocr_mode()
    validate_bank_card_upload(file)
    image_path = save_upload_file(file)
    try:
        validate_readable_image(image_path)
        try:
            quality = check_image_quality(str(image_path))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Uploaded file is not a readable image.") from exc
        ocr_text = recognize_text(str(image_path), mode=ocr_mode)
        fields = parse_bank_card_fields("\n".join(ocr_text))
        review_result = review_bank_card(fields, quality)
        return {
            "review_result": review_result,
            "quality": quality,
            "ocr_text": ocr_text,
            "fields": fields,
        }
    finally:
        image_path.unlink(missing_ok=True)


def review_id_card(side: str, fields: dict, quality: dict) -> str:
    if side == "unknown":
        return "review"
    if quality.get("quality_result") != "pass":
        return "review"

    if side == "front":
        required = ("name", "gender", "nation", "birth", "address", "id_number")
    else:
        required = ("issue_authority", "valid_period")

    if not all(fields.get(field) for field in required):
        return "review"
    return "pass"


@app.post("/id-card/review")
def review_id_card_image(file: UploadFile = File(...)) -> dict:
    ocr_mode = get_ocr_mode()
    image_path = save_upload_file(file)
    try:
        quality = check_image_quality(str(image_path))
        ocr_text = recognize_text(str(image_path), mode=ocr_mode)
        parsed = parse_id_card_fields("\n".join(ocr_text))
        side = str(parsed["side"])
        fields = parsed["fields"]
        review_result = review_id_card(side, fields, quality) if isinstance(fields, dict) else "review"
        return {
            "review_result": review_result,
            "side": side,
            "quality": quality,
            "ocr_text": ocr_text,
            "fields": fields,
        }
    finally:
        image_path.unlink(missing_ok=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
