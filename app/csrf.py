"""Session-bound CSRF token helpers."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request


CSRF_SESSION_KEY = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_FAILURE_DETAIL = "CSRF validation failed."


def get_or_create_csrf_token(request: Request) -> str:
    """Return the current Session token, creating a secure random token if needed."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def rotate_csrf_token(request: Request) -> str:
    """Replace and return the Session token."""
    token = secrets.token_urlsafe(32)
    request.session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf_token(
    request: Request,
    submitted_token: str | None,
) -> None:
    """Raise a JSON 403 response unless the submitted token matches the Session."""
    session_token = request.session.get(CSRF_SESSION_KEY)
    valid = (
        isinstance(session_token, str)
        and bool(session_token)
        and isinstance(submitted_token, str)
        and bool(submitted_token)
        and secrets.compare_digest(session_token, submitted_token)
    )
    if not valid:
        raise HTTPException(status_code=403, detail=CSRF_FAILURE_DETAIL)


def validate_csrf_request(request: Request) -> None:
    """Validate the standard CSRF request header."""
    validate_csrf_token(request, request.headers.get(CSRF_HEADER_NAME))
