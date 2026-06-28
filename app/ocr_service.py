"""OCR service integration layer."""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
PADDLEX_CACHE_DIR = ROOT_DIR / "reports" / "paddlex-runtime-cache"
OCR_TEMP_DIR = ROOT_DIR / "reports" / "ocr-temp"


def _extract_text_lines(ocr_result: Any) -> list[str]:
    """Extract recognized text from PaddleOCR v2/v3 style results."""
    lines: list[str] = []

    def collect(value: Any) -> None:
        if value is None:
            return

        if isinstance(value, dict):
            rec_texts = value.get("rec_texts")
            if isinstance(rec_texts, list):
                lines.extend(str(item).strip() for item in rec_texts if str(item).strip())
                return

            for key in ("res", "data"):
                if key in value:
                    collect(value[key])
                    return

            for item in value.values():
                collect(item)
            return

        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
            text = value[0].strip()
            if text:
                lines.append(text)
            return

        if isinstance(value, (list, tuple)):
            for item in value:
                collect(item)
            return

        result_json = getattr(value, "json", None)
        if callable(result_json):
            collect(result_json())
            return

        result_res = getattr(value, "res", None)
        if result_res is not None:
            collect(result_res)

    collect(ocr_result)
    return lines


@lru_cache(maxsize=1)
def _get_ocr_engine() -> Any:
    OCR_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(PADDLEX_CACHE_DIR))
    os.environ.setdefault("TMP", str(OCR_TEMP_DIR))
    os.environ.setdefault("TEMP", str(OCR_TEMP_DIR))
    os.environ.setdefault("TMPDIR", str(OCR_TEMP_DIR))
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("FLAGS_use_onednn", "0")
    tempfile.tempdir = str(OCR_TEMP_DIR)

    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError(
            "Real OCR requires PaddleOCR. Install it in the active environment: "
            "python -m pip install paddleocr"
        ) from exc

    try:
        return PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_mkldnn=False,
        )
    except TypeError:
        return PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)


def recognize_text(image_path: str) -> list[str]:
    """Return OCR text detected from the uploaded image.

    PaddleOCR 3.x exposes ``predict`` while older 2.x releases expose ``ocr``.
    The output shape differs by version, so text extraction is normalized here.
    """
    ocr_engine = _get_ocr_engine()
    if hasattr(ocr_engine, "predict"):
        result = ocr_engine.predict(image_path)
    else:
        result = ocr_engine.ocr(image_path, cls=True)

    return _extract_text_lines(result)
