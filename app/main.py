"""Application entry point for the bank OCR test platform."""

from __future__ import annotations

import shutil
from uuid import uuid4
from pathlib import Path

from fastapi import FastAPI, File, UploadFile

from app.field_parser import parse_bank_card_fields
from app.ocr_service import recognize_text
from app.quality_check import check_image_quality
from app.rule_check import review_bank_card


app = FastAPI(title="Bank OCR Test Platform")
ROOT_DIR = Path(__file__).resolve().parents[1]
UPLOAD_DIR = ROOT_DIR / "reports" / "tmp_uploads"


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Bank OCR test platform"}


@app.post("/bank-card/review")
def review_bank_card_image(file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename or "upload.png").suffix or ".png"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    image_path = UPLOAD_DIR / f"{uuid4().hex}{suffix}"
    with image_path.open("wb") as temp_file:
        shutil.copyfileobj(file.file, temp_file)

    try:
        quality = check_image_quality(str(image_path))
        ocr_text = recognize_text(str(image_path))
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
