"""Browser sessions survive local reloads without weakening auth or CSRF."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app import main
from app.auth_routes import router as auth_router
from app.page_routes import router as page_router
from app.users import create_user


@pytest.fixture
def local_key(monkeypatch, tmp_path):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    path = tmp_path / "reports" / ".session-secret"
    monkeypatch.setattr(main, "SESSION_SECRET_PATH", path, raising=False)
    return path


def fresh_app():
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=main._get_session_secret(),
                       session_cookie="bank_ocr_session", same_site="lax")
    app.include_router(auth_router)
    app.include_router(page_router)
    return app


def test_local_session_key_is_reused_after_app_recreation(local_key):
    first = main._get_session_secret()
    assert len(first) >= 32
    assert main._get_session_secret() == first
    assert local_key.read_text(encoding="ascii").strip() == first


def test_explicit_session_secret_takes_priority_without_creating_a_file(local_key, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-only-explicit-stable-key")
    assert main._get_session_secret() == "test-only-explicit-stable-key"
    assert not local_key.exists()


def test_simultaneous_workers_share_one_key(local_key):
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: main._get_session_secret(), range(16)))
    assert len(set(keys)) == 1
    assert local_key.read_text(encoding="ascii").strip() == keys[0]


def test_invalid_key_is_not_silently_replaced(local_key):
    local_key.parent.mkdir()
    local_key.write_text("", encoding="ascii")
    with pytest.raises(RuntimeError, match="Local session key"):
        main._get_session_secret()
    assert local_key.read_text(encoding="ascii") == ""


def test_admin_cookie_and_csrf_survive_another_local_instance(local_key, auth_db_path):
    create_user(username="session-test-admin", password="Session-test-password", role="admin")
    with TestClient(fresh_app(), base_url="http://testserver:8001") as first:
        token = first.get("/csrf-token").json()["csrf_token"]
        response = first.post("/login", data={"username": "session-test-admin", "password": "Session-test-password"},
                              headers={"X-CSRF-Token": token}, follow_redirects=False)
        assert response.headers["location"] == "/admin/reviews"
        rotated_token = first.get("/csrf-token").json()["csrf_token"]
        with TestClient(fresh_app(), base_url="http://testserver:8002") as restarted:
            restarted.cookies.update(first.cookies)
            for path in ("/user/bank-card", "/user/id-card", "/admin/reviews", "/admin/reviews/example-id"):
                assert restarted.get(path, follow_redirects=False).status_code == 200
            assert restarted.get("/csrf-token").json()["csrf_token"] == rotated_token
            assert restarted.post("/logout").status_code == 403
            assert restarted.post("/logout", headers={"X-CSRF-Token": rotated_token}, follow_redirects=False).status_code == 303
            assert restarted.get("/admin/reviews", follow_redirects=False).headers["location"] == "/login"
