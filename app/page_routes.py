"""Static portal page routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth_routes import require_active_user


router = APIRouter()
ROOT_DIR = Path(__file__).resolve().parents[1]
PORTAL_DIR = ROOT_DIR / "app" / "static" / "portal"


def _read_portal_page(filename: str) -> HTMLResponse:
    html = (PORTAL_DIR / filename).read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@router.get("/user", response_class=HTMLResponse)
def user_home(request: Request) -> Response:
    redirect = require_active_user(request)
    if redirect is not None:
        return redirect
    return _read_portal_page("user_home.html")


@router.get("/user/bank-card", response_class=HTMLResponse)
def user_bank_card(request: Request) -> Response:
    redirect = require_active_user(request)
    if redirect is not None:
        return redirect
    return _read_portal_page("user_bank_card.html")


@router.get("/user/id-card", response_class=HTMLResponse)
def user_id_card(request: Request) -> Response:
    redirect = require_active_user(request)
    if redirect is not None:
        return redirect
    return _read_portal_page("user_id_card.html")


@router.get("/admin/reviews", response_class=HTMLResponse)
def admin_reviews() -> HTMLResponse:
    return _read_portal_page("admin_reviews.html")


@router.get("/admin/reviews/{request_id}", response_class=HTMLResponse)
def admin_review_detail(request_id: str) -> HTMLResponse:
    return _read_portal_page("admin_review_detail.html")
