"""Static integration checks for user portal upload interactions."""

from fastapi.testclient import TestClient

def test_bank_card_page_references_interaction_script(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get("/user/bank-card")

    assert response.status_code == 200
    assert 'src="/static/portal/user_bank_card.js"' in response.text


def test_id_card_page_references_interaction_script(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get("/user/id-card")

    assert response.status_code == 200
    assert 'src="/static/portal/user_id_card.js"' in response.text


def test_user_portal_javascript_assets_are_available(
    isolated_auth_client: TestClient,
) -> None:
    for path in (
        "/static/portal/user_bank_card.js",
        "/static/portal/user_id_card.js",
    ):
        response = isolated_auth_client.get(path)

        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]


def test_bank_card_page_has_single_file_input_and_submit_button(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get("/user/bank-card")

    assert response.text.count('type="file"') == 1
    assert 'id="bankCardFileInput"' in response.text
    assert 'name="file"' in response.text
    assert 'id="bankCardSubmitButton"' in response.text
    assert 'id="bankCardRemoveButton"' in response.text


def test_id_card_page_has_distinct_front_and_back_inputs(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get("/user/id-card")

    assert response.text.count('type="file"') == 2
    assert 'id="idCardFrontFileInput"' in response.text
    assert 'name="front_file"' in response.text
    assert 'id="idCardBackFileInput"' in response.text
    assert 'name="back_file"' in response.text
    assert 'id="idCardSubmitButton"' in response.text
    assert 'id="idCardFrontRemoveButton"' in response.text
    assert 'id="idCardBackRemoveButton"' in response.text
    assert "请分别上传人像面和国徽面" in response.text


def test_user_pages_do_not_contain_debug_output_headings(
    authenticated_client: TestClient,
) -> None:
    forbidden_headings = ("OCR 原始文本", "响应 JSON", "完整字段")

    for path in ("/user/bank-card", "/user/id-card"):
        response = authenticated_client.get(path)

        for heading in forbidden_headings:
            assert heading not in response.text


def test_user_scripts_do_not_render_sensitive_review_payloads(
    isolated_auth_client: TestClient,
) -> None:
    forbidden_rendering = (
        "data.fields",
        "data.ocr_text",
        "data.quality",
        "JSON.stringify(data",
    )

    for path in (
        "/static/portal/user_bank_card.js",
        "/static/portal/user_id_card.js",
    ):
        script = isolated_auth_client.get(path).text

        for expression in forbidden_rendering:
            assert expression not in script


def test_interaction_scripts_use_existing_single_file_endpoints(
    isolated_auth_client: TestClient,
) -> None:
    bank_script = isolated_auth_client.get("/static/portal/user_bank_card.js").text
    id_script = isolated_auth_client.get("/static/portal/user_id_card.js").text

    assert 'fetchWithCsrf("/bank-card/review"' in bank_script
    assert 'fetchWithCsrf("/id-card/review"' in id_script
    assert 'formData.append("file"' in bank_script
    assert 'formData.append("file"' in id_script
    assert "Promise.all([submitSide(front), submitSide(back)])" in id_script
    assert "!front.selectedFile || !back.selectedFile" in id_script


def test_interaction_scripts_handle_preview_lifecycle_safely(
    isolated_auth_client: TestClient,
) -> None:
    for path in (
        "/static/portal/user_bank_card.js",
        "/static/portal/user_id_card.js",
    ):
        script = isolated_auth_client.get(path).text

        assert "URL.createObjectURL" in script
        assert "URL.revokeObjectURL" in script
        assert "textContent" in script
        assert "innerHTML" not in script
        assert "localStorage" not in script
        assert "sessionStorage" not in script
        assert "console.log" not in script


def test_existing_bank_card_debug_page_remains_available(
    isolated_auth_client: TestClient,
) -> None:
    response = isolated_auth_client.get("/bank-card/ui")

    assert response.status_code == 200
