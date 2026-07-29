"""Static integration checks for administrator review-record pages."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)
ADMIN_SCRIPT_PATHS = (
    "/static/portal/admin_reviews.js",
    "/static/portal/admin_review_detail.js",
)
def test_admin_list_page_references_interaction_script() -> None:
    response = client.get("/admin/reviews")

    assert response.status_code == 200
    assert 'src="/static/portal/admin_reviews.js"' in response.text


def test_admin_detail_page_references_interaction_script() -> None:
    response = client.get("/admin/reviews/test-request-id")

    assert response.status_code == 200
    assert 'src="/static/portal/admin_review_detail.js"' in response.text


@pytest.mark.parametrize("path", ADMIN_SCRIPT_PATHS)
def test_admin_javascript_assets_are_available(path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_admin_list_script_calls_review_records_endpoint() -> None:
    script = client.get("/static/portal/admin_reviews.js").text

    assert '"/review-records"' in script
    assert "`/review-records?${query}`" in script
    assert 'params.set("doc_type"' in script
    assert 'params.set("review_result"' in script


def test_admin_detail_script_calls_encoded_review_record_endpoint() -> None:
    script = client.get("/static/portal/admin_review_detail.js").text

    assert "decodeURIComponent(encodedRequestId)" in script
    assert "fetch(`/review-records/${encodeURIComponent(requestId)}`)" in script


def test_admin_list_page_contains_filter_controls() -> None:
    response = client.get("/admin/reviews")

    assert 'id="adminDocTypeFilter"' in response.text
    assert 'name="doc_type"' in response.text
    assert 'id="adminReviewResultFilter"' in response.text
    assert 'name="review_result"' in response.text
    assert 'id="adminFilterButton"' in response.text
    assert 'id="adminResetButton"' in response.text


def test_admin_list_page_contains_request_id_search() -> None:
    response = client.get("/admin/reviews")

    assert 'id="adminRequestSearchForm"' in response.text
    assert 'id="adminRequestIdSearch"' in response.text
    assert 'name="request_id"' in response.text


def test_admin_list_table_contains_required_columns() -> None:
    response = client.get("/admin/reviews")

    for heading in (
        "创建时间",
        "request_id",
        "证件类型",
        "文件名",
        "OCR 模式",
        "审核结果",
        "操作",
    ):
        assert f"<th>{heading}</th>" in response.text


def test_admin_detail_page_contains_persisted_record_fields() -> None:
    response = client.get("/admin/reviews/test-request-id")

    field_ids = (
        "detailRequestId",
        "detailDocType",
        "detailFilename",
        "detailOcrMode",
        "detailReviewResult",
        "detailQualityResult",
        "detailQualityReasons",
        "detailReviewReasons",
        "detailFieldsJson",
        "detailErrorMessage",
        "detailCreatedAt",
    )
    for field_id in field_ids:
        assert f'id="{field_id}"' in response.text


def test_admin_detail_page_does_not_claim_unpersisted_data() -> None:
    response = client.get("/admin/reviews/test-request-id")

    for heading in ("OCR 原始文本", "原始图片", "身份证面", "完整质量指标"):
        assert heading not in response.text


def test_admin_pages_keep_authentication_warning() -> None:
    for path in ("/admin/reviews", "/admin/reviews/test-request-id"):
        response = client.get(path)

        assert "当前管理员页面尚未接入登录鉴权" in response.text


@pytest.mark.parametrize("path", ADMIN_SCRIPT_PATHS)
def test_admin_scripts_render_backend_data_safely(path: str) -> None:
    script = client.get(path).text

    assert "textContent" in script
    assert "innerHTML" not in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "console.log" not in script
    assert "console.error" not in script


def test_admin_list_script_supports_empty_loading_error_and_detail_link_states() -> None:
    script = client.get("/static/portal/admin_reviews.js").text

    assert "暂无符合条件的审核记录" in script
    assert "正在加载审核记录" in script
    assert "审核记录加载失败，请稍后重试" in script
    assert "`/admin/reviews/${encodeURIComponent(requestId)}`" in script
    assert "if (!requestId)" in script


def test_admin_detail_script_handles_not_found_reasons_and_fields_json() -> None:
    script = client.get("/static/portal/admin_review_detail.js").text

    assert "response.status === 404" in script
    assert "未找到该审核记录" in script
    assert 'item.textContent = "无"' in script
    assert "JSON.parse(value)" in script
    assert "Object.entries(fields)" in script
