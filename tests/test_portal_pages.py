"""Tests for the static user and administrator portal pages."""

import pytest
from fastapi.testclient import TestClient

USER_PORTAL_ROUTES = [
    "/user",
    "/user/bank-card",
    "/user/id-card",
]
ADMIN_PORTAL_ROUTES = [
    "/admin/reviews",
    "/admin/reviews/test-request-id",
]
PORTAL_ROUTES = USER_PORTAL_ROUTES + ADMIN_PORTAL_ROUTES


@pytest.mark.parametrize("path", PORTAL_ROUTES)
def test_portal_page_returns_html(
    request: pytest.FixtureRequest,
    path: str,
) -> None:
    fixture_name = (
        "authenticated_admin_client"
        if path in ADMIN_PORTAL_ROUTES
        else "authenticated_client"
    )
    client: TestClient = request.getfixturevalue(fixture_name)
    response = client.get(path)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.parametrize("path", PORTAL_ROUTES)
def test_portal_page_uses_shared_stylesheet(
    request: pytest.FixtureRequest,
    path: str,
) -> None:
    fixture_name = (
        "authenticated_admin_client"
        if path in ADMIN_PORTAL_ROUTES
        else "authenticated_client"
    )
    client: TestClient = request.getfixturevalue(fixture_name)
    response = client.get(path)

    assert 'href="/static/portal/portal.css"' in response.text


def test_existing_bank_card_ui_still_returns_html(
    isolated_auth_client: TestClient,
) -> None:
    response = isolated_auth_client.get("/bank-card/ui")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_user_home_contains_business_links(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get("/user")

    assert 'href="/user/bank-card"' in response.text
    assert 'href="/user/id-card"' in response.text
    assert "银行卡认证" in response.text
    assert "身份证认证" in response.text


def test_id_card_page_describes_front_and_back_upload_capability(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get("/user/id-card")

    assert "分别上传身份证人像面和国徽面" in response.text
    assert "请分别上传人像面和国徽面" in response.text


def test_admin_pages_show_authorization_boundary(
    authenticated_admin_client: TestClient,
) -> None:
    for path in ("/admin/reviews", "/admin/reviews/test-request-id"):
        response = authenticated_admin_client.get(path)

        assert "当前页面已限制管理员账号访问" in response.text
        assert "审核记录接口尚未完成接口级鉴权" in response.text


def test_primary_portal_pages_use_precision_workspace_design(
    authenticated_client: TestClient,
    authenticated_admin_client: TestClient,
) -> None:
    expected_markers = {
        "/user": 'class="service-list"',
        "/user/bank-card": 'class="workbench-grid bank-workbench"',
        "/user/id-card": 'class="workbench-grid id-workbench"',
        "/admin/reviews": 'class="admin-toolbar"',
    }

    for path, marker in expected_markers.items():
        client = (
            authenticated_admin_client
            if path in ADMIN_PORTAL_ROUTES
            else authenticated_client
        )
        response = client.get(path)

        assert '<span class="brand-code">BOCR</span>' in response.text
        assert marker in response.text


def test_bank_card_page_uses_safe_synthetic_preview_asset(
    authenticated_client: TestClient,
) -> None:
    page = authenticated_client.get("/user/bank-card")
    asset = authenticated_client.get("/static/portal/assets/synthetic-bank-card.png")

    assert 'src="/static/portal/assets/synthetic-bank-card.png"' in page.text
    assert asset.status_code == 200
    assert asset.headers["content-type"] == "image/png"
