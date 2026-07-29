"""Application entry point for the bank OCR test platform."""

from __future__ import annotations

import logging
import os
import secrets
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from starlette.middleware.sessions import SessionMiddleware

from app.auth_routes import router as auth_router
from app.field_parser import parse_bank_card_fields
from app.id_card_parser import parse_id_card_fields
from app.logging_utils import mask_sensitive_data, sanitize_for_log
from app.ocr_service import recognize_text
from app.page_routes import router as page_router
from app.quality_check import check_image_quality, get_quality_reasons
from app.review_records import get_review_record, list_review_records, save_review_record
from app.rule_check import review_bank_card_with_reasons


logger = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parents[1]
UPLOAD_DIR = ROOT_DIR / "reports" / "tmp_uploads"
STATIC_DIR = ROOT_DIR / "app" / "static"
ALLOWED_BANK_CARD_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
ALLOWED_OCR_MODES = {"mock", "paddle"}
REVIEW_PATHS = {"/bank-card/review", "/id-card/review"}


def _get_session_secret() -> str:
    configured_secret = os.getenv("SESSION_SECRET")
    if configured_secret:
        return configured_secret
    logger.warning(
        "SESSION_SECRET is not set; using an ephemeral development session key."
    )
    return secrets.token_urlsafe(32)


app = FastAPI(title="Bank OCR Test Platform")
app.add_middleware(
    SessionMiddleware,
    secret_key=_get_session_secret(),
    session_cookie="bank_ocr_session",
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(auth_router)
app.include_router(page_router)


def _quality_reasons(quality: dict) -> list[str]:
    return get_quality_reasons(quality)


def _error_reason(detail: object) -> str:
    message = str(detail)
    if "Unsupported file type" in message:
        return "invalid_file_type"
    if "not a readable image" in message or "file is empty" in message:
        return "unreadable_image"
    if "Invalid OCR_MODE" in message:
        return "invalid_ocr_mode"
    return "internal_error"


def _save_audit_record(
    request: Request,
    *,
    doc_type: str,
    filename: str,
    ocr_mode: str | None,
    review_result: str,
    review_reasons: list[str],
    quality: dict,
    fields: dict,
    error_message: str | None = None,
) -> None:
    request_id = request.state.request_id
    save_review_record(
        request_id=request_id,
        doc_type=doc_type,
        filename=filename,
        ocr_mode=ocr_mode,
        review_result=review_result,
        quality_result=quality.get("quality_result"),
        quality_reasons=_quality_reasons(quality),
        review_reasons=review_reasons,
        fields=fields,
        error_message=error_message,
    )
    request.state.record_saved = True
    logger.info("record saved request_id=%s doc_type=%s", request_id, doc_type)


@app.middleware("http")
async def add_review_request_id(request: Request, call_next):
    if request.url.path not in REVIEW_PATHS:
        return await call_next(request)

    request.state.request_id = uuid4().hex
    request.state.record_saved = False
    logger.info(
        "request received request_id=%s path=%s",
        request.state.request_id,
        request.url.path,
    )
    response = await call_next(request)
    if response.status_code >= 400 and not request.state.record_saved:
        doc_type = "bank_card" if request.url.path == "/bank-card/review" else "id_card"
        try:
            _save_audit_record(
                request,
                doc_type=doc_type,
                filename="",
                ocr_mode=None,
                review_result="error",
                review_reasons=["invalid_request" if response.status_code < 500 else "internal_error"],
                quality={},
                fields={},
                error_message=f"HTTP {response.status_code}",
            )
        except Exception as exc:
            logger.error(
                "record save failed request_id=%s error=%s",
                request.state.request_id,
                mask_sensitive_data(str(exc)),
            )
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(HTTPException)
async def review_http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    content: dict[str, object] = {"detail": exc.detail}
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        content["request_id"] = request_id
        content["review_reasons"] = [_error_reason(exc.detail)]
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(content),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def review_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    content: dict[str, object] = {"detail": exc.errors()}
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        content["request_id"] = request_id
        content["review_reasons"] = ["invalid_request"]
    return JSONResponse(status_code=422, content=jsonable_encoder(content))


@app.exception_handler(Exception)
async def review_unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        logger.error(
            "review failed request_id=%s error=%s",
            request_id,
            mask_sensitive_data(str(exc)),
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error.",
                "request_id": request_id,
                "review_reasons": ["internal_error"],
            },
        )
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


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
def review_bank_card_image(request: Request, file: UploadFile = File(...)) -> dict:
    request_id = request.state.request_id
    filename = file.filename or ""
    ocr_mode: str | None = None
    image_path: Path | None = None
    quality: dict = {}
    fields: dict = {}
    try:
        try:
            ocr_mode = get_ocr_mode()
            logger.info(
                "file validation request_id=%s filename=%s",
                request_id,
                mask_sensitive_data(filename),
            )
            validate_bank_card_upload(file)
            image_path = save_upload_file(file)
            validate_readable_image(image_path)
            try:
                quality = check_image_quality(str(image_path))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Uploaded file is not a readable image.") from exc
            quality_reasons = _quality_reasons(quality)
            quality["quality_reasons"] = quality_reasons
            logger.info(
                "quality check request_id=%s result=%s reasons=%s",
                request_id,
                quality.get("quality_result"),
                quality_reasons,
            )
            ocr_text = recognize_text(str(image_path), mode=ocr_mode)
            logger.info("ocr request_id=%s mode=%s line_count=%s", request_id, ocr_mode, len(ocr_text))
            fields = parse_bank_card_fields("\n".join(ocr_text))
            logger.info("field parse request_id=%s fields=%s", request_id, sanitize_for_log(fields))
            review_result, review_reasons = review_bank_card_with_reasons(fields, quality)
            logger.info(
                "rule check request_id=%s result=%s reasons=%s",
                request_id,
                review_result,
                review_reasons,
            )
        except HTTPException as exc:
            error_message = str(exc.detail)
            review_reasons = [_error_reason(exc.detail)]
            logger.warning(
                "review failed request_id=%s error=%s",
                request_id,
                mask_sensitive_data(error_message),
            )
            _save_audit_record(
                request,
                doc_type="bank_card",
                filename=filename,
                ocr_mode=ocr_mode,
                review_result="error",
                review_reasons=review_reasons,
                quality=quality,
                fields=fields,
                error_message=error_message,
            )
            raise

        _save_audit_record(
            request,
            doc_type="bank_card",
            filename=filename,
            ocr_mode=ocr_mode,
            review_result=review_result,
            review_reasons=review_reasons,
            quality=quality,
            fields=fields,
        )
        return {
            "request_id": request_id,
            "review_result": review_result,
            "review_reasons": review_reasons,
            "quality": quality,
            "ocr_text": ocr_text,
            "fields": fields,
        }
    finally:
        if image_path:
            image_path.unlink(missing_ok=True)


def review_id_card_with_reasons(side: str, fields: dict, quality: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if side == "unknown":
        reasons.append("unknown_id_card_side")
    if side == "front":
        required = ("name", "gender", "nation", "birth", "address", "id_number")
    elif side == "back":
        required = ("issue_authority", "valid_period")
    else:
        required = ()

    reasons.extend(f"missing_{field}" for field in required if not fields.get(field))
    reasons.extend(reason for reason in _quality_reasons(quality) if reason not in reasons)
    if reasons or quality.get("quality_result") != "pass":
        return "review", reasons
    return "pass", []


def review_id_card(side: str, fields: dict, quality: dict) -> str:
    review_result, _review_reasons = review_id_card_with_reasons(side, fields, quality)
    return review_result


@app.post("/id-card/review")
def review_id_card_image(request: Request, file: UploadFile = File(...)) -> dict:
    request_id = request.state.request_id
    filename = file.filename or ""
    ocr_mode: str | None = None
    image_path: Path | None = None
    quality: dict = {}
    fields: dict = {}
    try:
        try:
            ocr_mode = get_ocr_mode()
            logger.info(
                "file validation request_id=%s filename=%s",
                request_id,
                mask_sensitive_data(filename),
            )
            validate_bank_card_upload(file)
            image_path = save_upload_file(file)
            validate_readable_image(image_path)
            try:
                quality = check_image_quality(str(image_path))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Uploaded file is not a readable image.") from exc
            quality["quality_reasons"] = _quality_reasons(quality)
            logger.info(
                "quality check request_id=%s result=%s reasons=%s",
                request_id,
                quality.get("quality_result"),
                _quality_reasons(quality),
            )
            ocr_text = recognize_text(str(image_path), mode=ocr_mode)
            logger.info("ocr request_id=%s mode=%s line_count=%s", request_id, ocr_mode, len(ocr_text))
            parsed = parse_id_card_fields("\n".join(ocr_text))
            side = str(parsed["side"])
            parsed_fields = parsed["fields"]
            fields = parsed_fields if isinstance(parsed_fields, dict) else {}
            logger.info("field parse request_id=%s fields=%s", request_id, sanitize_for_log(fields))
            review_result, review_reasons = review_id_card_with_reasons(side, fields, quality)
            logger.info(
                "rule check request_id=%s result=%s reasons=%s",
                request_id,
                review_result,
                review_reasons,
            )
        except HTTPException as exc:
            error_message = str(exc.detail)
            review_reasons = [_error_reason(exc.detail)]
            logger.warning(
                "review failed request_id=%s error=%s",
                request_id,
                mask_sensitive_data(error_message),
            )
            _save_audit_record(
                request,
                doc_type="id_card",
                filename=filename,
                ocr_mode=ocr_mode,
                review_result="error",
                review_reasons=review_reasons,
                quality=quality,
                fields=fields,
                error_message=error_message,
            )
            raise

        _save_audit_record(
            request,
            doc_type="id_card",
            filename=filename,
            ocr_mode=ocr_mode,
            review_result=review_result,
            review_reasons=review_reasons,
            quality=quality,
            fields=fields,
        )
        return {
            "request_id": request_id,
            "review_result": review_result,
            "review_reasons": review_reasons,
            "side": side,
            "quality": quality,
            "ocr_text": ocr_text,
            "fields": fields,
        }
    finally:
        if image_path:
            image_path.unlink(missing_ok=True)


@app.get("/review-records")
def query_review_records(
    doc_type: str | None = Query(default=None),
    review_result: str | None = Query(default=None),
) -> list[dict]:
    return list_review_records(doc_type=doc_type, review_result=review_result)


@app.get("/review-records/{request_id}")
def read_review_record(request_id: str) -> dict:
    record = get_review_record(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Review record not found.")
    return record


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
# python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
# set OCR_MODE=paddle
