"""Minimal Locust scenario for POST /bank-card/review."""

from __future__ import annotations

from pathlib import Path

from locust import HttpUser, between, task


ROOT_DIR = Path(__file__).resolve().parents[1]
BANK_CARD_IMAGE_PATH = ROOT_DIR / "data" / "processed" / "bank_card" / "normal" / "bank_card_0001.png"


class BankCardReviewUser(HttpUser):
    """Upload one fixed synthetic bank-card image to the review endpoint."""

    host = "http://127.0.0.1:8000"
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.image_bytes = BANK_CARD_IMAGE_PATH.read_bytes() if BANK_CARD_IMAGE_PATH.is_file() else None

    @task
    def review_bank_card(self) -> None:
        if self.image_bytes is None:
            self.environment.events.request.fire(
                request_type="POST",
                name="/bank-card/review",
                response_time=0,
                response_length=0,
                exception=FileNotFoundError(f"Missing test image: {BANK_CARD_IMAGE_PATH}"),
            )
            return

        files = {
            "file": (
                BANK_CARD_IMAGE_PATH.name,
                self.image_bytes,
                "image/png",
            )
        }
        with self.client.post("/bank-card/review", files=files, name="/bank-card/review", catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"Expected HTTP 200, got {response.status_code}: {response.text[:200]}")
                return

            try:
                data = response.json()
            except ValueError as exc:
                response.failure(f"Response is not JSON: {exc}")
                return

            if "review_result" not in data:
                response.failure("Response JSON missing review_result")
                return

            response.success()
