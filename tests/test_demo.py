"""Day 1 pytest exercises for the bank OCR test platform."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from api_client import ApiClient, BankOcrApiClient, load_json_file, run_command, save_json_file

ARTIFACT_DIR = Path("reports") / "test-artifacts" / "day1"


class FakeResponse:
    def __init__(self, status_code: int, json_data: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("response is not json")
        return self._json_data


@pytest.fixture()
def api_client() -> ApiClient:
    return ApiClient("http://127.0.0.1:8000", timeout=1.0)


@pytest.fixture()
def fake_request(monkeypatch):
    calls: list[dict[str, Any]] = []

    def install(status_code: int = 200, json_data: Any = None, text: str = "") -> list[dict[str, Any]]:
        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            calls.append({"method": method, "url": url, "kwargs": kwargs})
            return FakeResponse(status_code=status_code, json_data=json_data, text=text)

        monkeypatch.setattr(requests.Session, "request", request)
        return calls

    return install


def test_api_client_get_parses_json(api_client: ApiClient, fake_request) -> None:
    calls = fake_request(json_data={"message": "Bank OCR test platform"})

    response = api_client.get("/")

    assert response.ok is True
    assert response.status_code == 200
    assert response.json_data["message"] == "Bank OCR test platform"
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://127.0.0.1:8000/"


def test_project_client_inherits_base_client(fake_request) -> None:
    fake_request(json_data={"message": "Bank OCR test platform"})
    client = BankOcrApiClient("http://127.0.0.1:8000")

    response = client.health()

    assert isinstance(client, ApiClient)
    assert response.json_data == {"message": "Bank OCR test platform"}


def test_api_client_post_handles_non_json_response(api_client: ApiClient, fake_request) -> None:
    fake_request(status_code=500, json_data=None, text="Internal Server Error")

    response = api_client.post("/bank-card/review", files={"file": ("bank_card.png", b"fake", "image/png")})

    assert response.ok is False
    assert response.status_code == 500
    assert response.json_data is None
    assert "Internal Server Error" in response.text


@pytest.mark.parametrize(
    ("path", "expected_url"),
    [
        ("/bank-card/review", "http://127.0.0.1:8000/bank-card/review"),
        ("id-card/review", "http://127.0.0.1:8000/id-card/review"),
        ("http://example.test/health", "http://example.test/health"),
    ],
)
def test_api_client_builds_urls(api_client: ApiClient, path: str, expected_url: str) -> None:
    assert api_client._build_url(path) == expected_url


def test_api_client_captures_request_exception(api_client: ApiClient, monkeypatch) -> None:
    def raise_timeout(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        raise requests.Timeout("request timed out")

    monkeypatch.setattr(requests.Session, "request", raise_timeout)

    response = api_client.get("/")

    assert response.ok is False
    assert response.status_code is None
    assert "request timed out" in str(response.error)


@pytest.mark.smoke
def test_json_file_helpers_and_subprocess() -> None:
    report_path = ARTIFACT_DIR / "result.json"
    save_json_file(report_path, {"review_result": "pass", "case_count": 5})

    data = load_json_file(report_path)
    command_result = run_command(["python", "--version"])

    assert data["review_result"] == "pass"
    assert data["case_count"] == 5
    assert command_result.returncode == 0
