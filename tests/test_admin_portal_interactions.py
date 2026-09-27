"""Static integration checks for administrator review-record pages."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_SCRIPT_PATHS = (
    "/static/portal/admin_reviews.js",
    "/static/portal/admin_review_detail.js",
)


def test_admin_list_page_references_interaction_script(
    authenticated_admin_client: TestClient,
) -> None:
    response = authenticated_admin_client.get("/admin/reviews")

    assert response.status_code == 200
    assert 'src="/static/portal/admin_reviews.js"' in response.text


def test_admin_detail_page_references_interaction_script(
    authenticated_admin_client: TestClient,
) -> None:
    response = authenticated_admin_client.get("/admin/reviews/test-request-id")

    assert response.status_code == 200
    assert 'src="/static/portal/admin_review_detail.js"' in response.text


@pytest.mark.parametrize("path", ADMIN_SCRIPT_PATHS)
def test_admin_javascript_assets_are_available(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    response = isolated_auth_client.get(path)

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_admin_list_script_calls_review_records_endpoint(
    isolated_auth_client: TestClient,
) -> None:
    script = isolated_auth_client.get("/static/portal/admin_reviews.js").text

    assert '"/review-records"' in script
    assert "`/review-records?${query}`" in script
    assert 'params.set("doc_type"' in script
    assert 'params.set("review_result"' in script


def test_admin_detail_script_calls_encoded_review_record_endpoint(
    isolated_auth_client: TestClient,
) -> None:
    script = isolated_auth_client.get("/static/portal/admin_review_detail.js").text

    assert "decodeURIComponent(encodedRequestId)" in script
    assert "fetch(`/review-records/${encodeURIComponent(requestId)}`)" in script


def test_admin_list_page_contains_filter_controls(
    authenticated_admin_client: TestClient,
) -> None:
    response = authenticated_admin_client.get("/admin/reviews")

    assert 'id="adminDocTypeFilter"' in response.text
    assert 'name="doc_type"' in response.text
    assert 'id="adminReviewResultFilter"' in response.text
    assert 'name="review_result"' in response.text
    assert 'id="adminFilterButton"' in response.text
    assert 'id="adminResetButton"' in response.text


def test_admin_list_page_contains_request_id_search(
    authenticated_admin_client: TestClient,
) -> None:
    response = authenticated_admin_client.get("/admin/reviews")

    assert 'id="adminRequestSearchForm"' in response.text
    assert 'id="adminRequestIdSearch"' in response.text
    assert 'name="request_id"' in response.text


def test_admin_list_table_contains_required_columns(
    authenticated_admin_client: TestClient,
) -> None:
    response = authenticated_admin_client.get("/admin/reviews")

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


def test_admin_detail_page_contains_persisted_record_fields(
    authenticated_admin_client: TestClient,
) -> None:
    response = authenticated_admin_client.get("/admin/reviews/test-request-id")

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


def test_admin_detail_page_does_not_claim_unpersisted_data(
    authenticated_admin_client: TestClient,
) -> None:
    response = authenticated_admin_client.get("/admin/reviews/test-request-id")

    for heading in ("OCR 原始文本", "原始图片", "身份证面", "完整质量指标"):
        assert heading not in response.text


def test_admin_pages_show_page_and_api_authorization_boundary(
    authenticated_admin_client: TestClient,
) -> None:
    for path in ("/admin/reviews", "/admin/reviews/test-request-id"):
        response = authenticated_admin_client.get(path)

        assert "当前页面已限制管理员账号访问" in response.text
        assert "审核记录接口尚未完成接口级鉴权" in response.text


@pytest.mark.parametrize("path", ADMIN_SCRIPT_PATHS)
def test_admin_scripts_render_backend_data_safely(
    isolated_auth_client: TestClient,
    path: str,
) -> None:
    script = isolated_auth_client.get(path).text

    assert "textContent" in script
    assert "innerHTML" not in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "console.log" not in script
    assert "console.error" not in script


def test_admin_list_script_supports_empty_loading_error_and_detail_link_states(
    isolated_auth_client: TestClient,
) -> None:
    script = isolated_auth_client.get("/static/portal/admin_reviews.js").text

    assert "暂无符合条件的审核记录" in script
    assert "正在加载审核记录" in script
    assert "审核记录加载失败，请稍后重试" in script
    assert "`/admin/reviews/${encodeURIComponent(requestId)}`" in script
    assert "if (!requestId)" in script


def test_admin_detail_script_handles_not_found_reasons_and_fields_json(
    isolated_auth_client: TestClient,
) -> None:
    script = isolated_auth_client.get("/static/portal/admin_review_detail.js").text

    assert "response.status === 404" in script
    assert "未找到该审核记录" in script
    assert 'item.textContent = "无"' in script
    assert "JSON.parse(value)" in script
    assert "Object.entries(fields)" in script


def test_admin_detail_page_renders_the_agent_trace_panel(
    authenticated_admin_client: TestClient,
) -> None:
    """P3 的 Agent 轨迹面板要有按钮、状态位与三个渲染容器。

    与上面的 P0 解释面板并列，但**不是同一个东西** —— P0 走固定流水线，
    这个走 Agent 循环，返回结构不同（多 trace/budget/stop_reason）。
    """
    response = authenticated_admin_client.get("/admin/reviews/abc123")

    assert 'id="agentTraceButton"' in response.text
    assert 'id="agentTraceStatus"' in response.text
    assert 'id="agentTraceList"' in response.text
    assert 'id="agentBudgetMeta"' in response.text


def test_admin_detail_script_calls_the_agent_endpoint_with_csrf(
    isolated_auth_client: TestClient,
) -> None:
    """必须打到 /ai/agent/explain，且走 CSRF 路径（它是 POST）。"""
    script = isolated_auth_client.get("/static/portal/admin_review_detail.js").text

    assert "/ai/agent/explain/${encodeURIComponent(currentRequestId)}" in script
    assert script.count("fetchWithCsrf") >= 2


def test_agent_trace_renders_rejected_steps_distinctly(
    isolated_auth_client: TestClient,
) -> None:
    """被拒步骤要有独立状态 —— 那是「Agent 有没有按预期行事」的看点。

    被拒（executed=false）与执行失败（ok=false）语义不同：前者是安全边界
    起了作用，后者是工具本身出错。混在一起会看不出区别。
    """
    script = isolated_auth_client.get("/static/portal/admin_review_detail.js").text

    assert 'entry.executed === false' in script
    assert "not_whitelisted" in script
    assert "invalid_params" in script


def test_agent_trace_does_not_reuse_the_p0_meta_renderer(
    isolated_auth_client: TestClient,
) -> None:
    """Agent 面板不能复用 P0 的 renderAiMeta。

    P0 的 meta 读 engine.generation / rewrite / rerank / confidence，
    这些在 Agent 响应里都不存在 —— 硬套会静默显示成「模板生成」「无」。
    """
    script = isolated_auth_client.get("/static/portal/admin_review_detail.js").text

    assert "function renderAgentMeta(" in script
    assert "function renderAgentBudget(" in script
