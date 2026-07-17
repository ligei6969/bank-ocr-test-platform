from dataclasses import dataclass
from typing import Any


import requests


@dataclass
class ApiResponse:
    status_code: int | None
    json_data: Any = None 
    text: str = ""
    error: str | None = None
# class ApiResponse:
#     def __init__(self, status_code, json_data = None , text = "",error = None):
#         self.status_code = status_code
#         self.json_data = json_data
#         self.text = text
#         self.error = error   
    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None and 200 <= self.status_code < 300


class ApiClient:
    """Thin requests wrapper with JSON parsing and exception capture."""

    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
    
    def get(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("POST", path, **kwargs)

    def request(self, method: str, path: str, **kwargs: Any) -> ApiResponse:
        url = self._build_url(path)
        try:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            return ApiResponse(status_code=None, error=str(exc))

        try:
            json_data = response.json()
        except ValueError:
            json_data = None
        
        return ApiResponse(
            status_code=response.status_code,
            json_data=json_data,
            text=response.text,
        )

    def _build_url(self, path : str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"
    
class BankOcrApiClient(ApiClient):
    

    def health(self) -> ApiResponse:
        return self.get("/")
    def review_bank_card(self, image_path: Path) -> ApiResponse:
        with open(image_path, "rb") as image_file:
            return self.post(
                "bank-card/review",
                files = {"file": (image_path.name, image_file, "image/png")}
            )
    def load_json_file(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))


def save_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


if __name__ == "__main__":
    client = ApiClient("http://127.0.0.1:8000")
    resp = client.get("/")
    print(resp.ok)
    print(resp.status_code)
    print(resp.json_data)
    print(resp.error)
    print(resp.text)
