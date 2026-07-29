"""Tests for the static user and administrator portal pages."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)
PORTAL_ROUTES = [
    "/user",
    "/user/bank-card",
    "/user/id-card",
    "/admin/reviews",
    "/admin/reviews/test-request-id",
]


@pytest.mark.parametrize("path", PORTAL_ROUTES)
def test_portal_page_returns_html(path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.parametrize("path", PORTAL_ROUTES)
def test_portal_page_uses_shared_stylesheet(path: str) -> None:
    response = client.get(path)

    assert 'href="/static/portal/portal.css"' in response.text


def test_existing_bank_card_ui_still_returns_html() -> None:
    response = client.get("/bank-card/ui")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_user_home_contains_business_links() -> None:
    response = client.get("/user")

    assert 'href="/user/bank-card"' in response.text
    assert 'href="/user/id-card"' in response.text
    assert "银行卡认证" in response.text
    assert "身份证认证" in response.text


def test_id_card_page_describes_front_and_back_upload_capability() -> None:
    response = client.get("/user/id-card")

    assert "分别上传身份证人像面和国徽面" in response.text
    assert "请分别上传人像面和国徽面" in response.text


def test_admin_pages_show_authentication_warning() -> None:
    for path in ("/admin/reviews", "/admin/reviews/test-request-id"):
        response = client.get(path)

        assert "尚未接入登录鉴权" in response.text


def test_primary_portal_pages_use_precision_workspace_design() -> None:
    expected_markers = {
        "/user": 'class="service-list"',
        "/user/bank-card": 'class="workbench-grid bank-workbench"',
        "/user/id-card": 'class="workbench-grid id-workbench"',
        "/admin/reviews": 'class="admin-toolbar"',
    }

    for path, marker in expected_markers.items():
        response = client.get(path)

        assert '<span class="brand-code">BOCR</span>' in response.text
        assert marker in response.text


def test_bank_card_page_uses_safe_synthetic_preview_asset() -> None:
    page = client.get("/user/bank-card")
    asset = client.get("/static/portal/assets/synthetic-bank-card.png")

    assert 'src="/static/portal/assets/synthetic-bank-card.png"' in page.text
    assert asset.status_code == 200
    assert asset.headers["content-type"] == "image/png"
