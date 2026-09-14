"""可插拔 LLM 适配层。

为什么要有这一层
----------------
审核平台的测试必须在 CI 里稳定、零成本、可重复。所以本服务里**每一个**会调用
LLM 的环节（查询改写、结果重排、解释生成）都必须能在没有 LLM 的情况下工作。

这里的做法和平台自身的 ``OCR_MODE=mock|paddle`` 是同一个套路：

* 没配 API key / ``LLM_PROVIDER=none`` → ``NullLLMClient``，
  ``available=False``，调用方走确定性降级分支；
* 配了 key → ``HttpLLMClient``，走真实模型。

这样「AI 服务需不需要联网」由环境变量单独控制，而不是由代码分支控制。

协议形态
--------
默认 ``LLM_PROVIDER=openai``，走 OpenAI 兼容的 ``POST {base}/chat/completions``。
国内大量中转站 / 自建网关都是这个形态，兼容面最广。
``LLM_PROVIDER=anthropic`` 时走 ``POST {base}/v1/messages``。

HTTP 客户端用的是标准库 ``urllib``，不引入任何第三方依赖；
在异步调用链里通过 ``asyncio.to_thread`` 执行，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_TIMEOUT_S = 20.0


class LLMUnavailableError(RuntimeError):
    """LLM 未配置或调用失败，调用方应切换到确定性降级分支。"""


class LLMClient(Protocol):
    """LLM 客户端协议。"""

    @property
    def available(self) -> bool:
        """是否可用。False 时调用方不得调用 ``complete``。"""

    @property
    def name(self) -> str:
        """用于把真正生效的引擎回传给前端，便于排查问题。"""

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        """返回模型输出的纯文本。失败时抛 ``LLMUnavailableError``。"""


@dataclass
class NullLLMClient:
    """未配置模型时的空实现：始终不可用，调用即抛异常。"""

    reason: str = "LLM not configured"

    @property
    def available(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return "none"

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        raise LLMUnavailableError(self.reason)


def _clean_text(value: Any) -> str:
    """移除 Unicode 代理字符，避免请求体编码失败。

    移植自 EchoMind ``MCPToolManager._clean_text``。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.encode("utf-8", errors="ignore").decode("utf-8")


@dataclass
class HttpLLMClient:
    """OpenAI 兼容 / Anthropic 两种协议的最小 HTTP 客户端。"""

    provider: str
    api_key: str
    model: str
    base_url: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_retries: int = 0
    _last_error: str | None = field(default=None, init=False, repr=False)

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return f"llm:{self.provider}:{self.model}"

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ── 请求构造 ──────────────────────────────────────────────────────────────

    def _build_request(self, prompt: str, system: str | None) -> urllib.request.Request:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.provider == "anthropic":
            url = f"{self.base_url.rstrip('/')}/v1/messages"
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = ANTHROPIC_VERSION
            payload: dict[str, Any] = {
                "model": self.model,
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                payload["system"] = system
        else:
            url = f"{self.base_url.rstrip('/')}/chat/completions"
            headers["Authorization"] = f"Bearer {self.api_key}"
            messages: list[dict[str, str]] = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            payload = {"model": self.model, "messages": messages}

        return urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    @staticmethod
    def _extract_text(provider: str, body: dict[str, Any]) -> str:
        if provider == "anthropic":
            blocks = body.get("content") or []
            return "".join(
                str(block.get("text", ""))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
        choices = body.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content", ""))

    # ── 同步实现（在线程池里跑）─────────────────────────────────────────────

    def _complete_sync(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
    ) -> str:
        request = self._build_request(prompt, system)
        # max_tokens / temperature 在 payload 阶段统一注入
        raw_payload = json.loads(request.data.decode("utf-8"))
        if self.provider == "anthropic":
            raw_payload["max_tokens"] = max_tokens
            raw_payload["temperature"] = temperature
        else:
            raw_payload["max_tokens"] = max_tokens
            raw_payload["temperature"] = temperature
        request.data = json.dumps(raw_payload, ensure_ascii=False).encode("utf-8")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 4xx/5xx
            detail = exc.read().decode("utf-8", errors="ignore")[:200]
            self._last_error = f"HTTP {exc.code}: {detail}"
            raise LLMUnavailableError(self._last_error) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._last_error = f"connection error: {exc}"
            raise LLMUnavailableError(self._last_error) from exc
        except json.JSONDecodeError as exc:
            self._last_error = "invalid JSON response"
            raise LLMUnavailableError(self._last_error) from exc

        text = self._extract_text(self.provider, body).strip()
        if not text:
            self._last_error = "empty completion"
            raise LLMUnavailableError(self._last_error)
        self._last_error = None
        return text

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        attempts = max(1, self.max_retries + 1)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await asyncio.to_thread(
                    self._complete_sync,
                    _clean_text(prompt),
                    _clean_text(system) if system else None,
                    max_tokens,
                    temperature,
                )
            except LLMUnavailableError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.2 * (attempt + 1))
        raise LLMUnavailableError(str(last_error) if last_error else "LLM unavailable")


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def build_llm_client(env: dict[str, str] | None = None) -> LLMClient:
    """根据环境变量构造 LLM 客户端；未配置时返回不可用的空实现。

    支持的环境变量::

        LLM_PROVIDER   openai | anthropic | none    默认 openai（有 key 时）
        LLM_API_KEY    也可用 OPENAI_API_KEY / ANTHROPIC_API_KEY
        LLM_BASE_URL   默认按 provider 取官方地址
        LLM_MODEL      默认 gpt-4o-mini / claude-3-5-sonnet-20241022
        LLM_TIMEOUT_S  默认 20
        LLM_MAX_RETRIES 默认 0（保持测试确定性）
    """
    source = env if env is not None else os.environ
    provider = (source.get("LLM_PROVIDER", "") or "").strip().lower()

    if provider == "none":
        return NullLLMClient(reason="LLM_PROVIDER=none")

    api_key = _first_env_from(source, "LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    if not api_key:
        return NullLLMClient(reason="no LLM API key configured")

    if not provider:
        # 有 key 但没显式指定 provider：按 key 前缀猜一个最可能的
        provider = "anthropic" if api_key.startswith("sk-ant") else "openai"
    if provider not in {"openai", "anthropic"}:
        return NullLLMClient(reason=f"unsupported LLM_PROVIDER={provider}")

    default_base = (
        DEFAULT_ANTHROPIC_BASE_URL if provider == "anthropic" else DEFAULT_OPENAI_BASE_URL
    )
    default_model = (
        DEFAULT_ANTHROPIC_MODEL if provider == "anthropic" else DEFAULT_OPENAI_MODEL
    )
    base_url = source.get("LLM_BASE_URL", "").strip() or default_base
    model = source.get("LLM_MODEL", "").strip() or default_model

    timeout_s = _safe_float(source.get("LLM_TIMEOUT_S"), DEFAULT_TIMEOUT_S, minimum=1.0)
    max_retries = int(_safe_float(source.get("LLM_MAX_RETRIES"), 0.0, minimum=0.0))

    return HttpLLMClient(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_s=timeout_s,
        max_retries=max_retries,
    )


def _first_env_from(source: dict[str, str], *names: str) -> str:
    for name in names:
        value = (source.get(name) or "").strip()
        if value:
            return value
    return ""


def _safe_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default
