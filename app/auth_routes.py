"""Login, logout, and session authentication helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.users import get_user_by_id, get_user_by_username, verify_password


router = APIRouter()
ROOT_DIR = Path(__file__).resolve().parents[1]
LOGIN_PAGE_PATH = ROOT_DIR / "app" / "static" / "portal" / "login.html"
FORBIDDEN_PAGE_PATH = ROOT_DIR / "app" / "static" / "portal" / "forbidden.html"
LOGIN_ERROR_LOCATION = "/login?error=1"
ADMIN_ROLE = "admin"


def get_active_session_user(request: Request) -> dict[str, Any] | None:
    """Return the current active user, clearing stale or invalid sessions."""
    user_id = request.session.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        if request.session:
            request.session.clear()
        return None

    user = get_user_by_id(user_id)
    if user is None or not user["is_active"]:
        request.session.clear()
        return None
    return user


def require_active_user(request: Request) -> RedirectResponse | None:
    """Redirect anonymous or stale sessions to the login page."""
    if get_active_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return None


def require_admin_user(request: Request) -> Response | None:
    """Require an active administrator without storing role in the session."""
    user = get_active_session_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user["role"] != ADMIN_ROLE:
        return HTMLResponse(
            content=FORBIDDEN_PAGE_PATH.read_text(encoding="utf-8"),
            status_code=403,
        )
    return None


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    user = get_active_session_user(request)
    if user is not None:
        return RedirectResponse(url="/user", status_code=303)
    return HTMLResponse(content=LOGIN_PAGE_PATH.read_text(encoding="utf-8"))


@router.post("/login")
def login(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
) -> RedirectResponse:
    normalized_username = username.strip()
    user = get_user_by_username(normalized_username) if normalized_username else None
    authenticated = bool(
        password
        and user is not None
        and user["is_active"]
        and verify_password(password, user["password_hash"])
    )

    request.session.clear()
    if not authenticated or user is None:
        return RedirectResponse(url=LOGIN_ERROR_LOCATION, status_code=303)

    request.session["user_id"] = user["id"]
    return RedirectResponse(url="/user", status_code=303)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
