"""DeepSeek request compatibility without network access or credentials."""

import json

import pytest

from ai_service.llm import HttpLLMClient, LLMUnavailableError


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_deepseek_disables_thinking_for_small_text_budgets(model):
    client = HttpLLMClient("openai", "synthetic-key", model, "https://api.deepseek.com/v1")
    payload = json.loads(client._build_request("Explain", None).data)
    assert payload["thinking"] == {"type": "disabled"}


def test_other_openai_compatible_services_do_not_receive_deepseek_options():
    client = HttpLLMClient("openai", "synthetic-key", "deepseek-flash", "https://example.com/v1")
    payload = json.loads(client._build_request("Explain", None).data)
    assert "thinking" not in payload


def test_reasoning_only_response_is_not_treated_as_final_answer(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {
                "content": None, "reasoning_content": "internal reasoning"
            }}]}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    client = HttpLLMClient("openai", "synthetic-key", "deepseek-flash", "https://api.deepseek.com")
    with pytest.raises(LLMUnavailableError, match="empty completion"):
        client._complete_sync("Explain", None, 512, 0.0)
