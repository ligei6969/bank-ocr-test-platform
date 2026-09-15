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

token 用量
----------
``complete()`` 只返回文本，但成本与预算需要 token 数。这里不把它塞进返回值
（那会改动所有调用点与全部测试替身），而是走一个**可选**的旁路：
客户端把「上一次调用」的用量暂存起来，调用方事后用 :func:`take_usage` 取走。

* provider 回了 ``usage`` → 用真实值（``source="provider"``）；
* 没回 / 客户端不支持 → ``take_usage`` 返回 ``None``，调用方退回字符估算。

两条路都走 ``LLMUsage`` 同一个结构，报告里能分清哪个是账单依据、哪个只是兜底。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_TIMEOUT_S = 20.0


class LLMUnavailableError(RuntimeError):
    """LLM 未配置或调用失败，调用方应切换到确定性降级分支。"""


#: 用量来自 provider 回传的真实 usage，可作计费依据。
USAGE_PROVIDER = "provider"
#: 用量是本地字符估算，只用于预算兜底，**不可作计费依据**。
USAGE_ESTIMATE = "estimate"


@dataclass(frozen=True)
class LLMUsage:
    """一次（或若干次）LLM 调用的 token 用量。

    刻意带 ``source`` 字段：真实用量与估算值必须能分辨。把估算值混进成本报告，
    等于给「成本可控」这个结论注水 —— 而面试里被追问「这是真实数还是估的」时，
    答不上来比没有这个指标更糟。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    source: str = USAGE_PROVIDER

    @property
    def total(self) -> int:
        return max(0, self.prompt_tokens) + max(0, self.completion_tokens)

    @property
    def estimated(self) -> bool:
        return self.source != USAGE_PROVIDER

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        # source 取「更弱」的那一个：只要有一次是估算，合计就不该被当作真实值
        source = (
            USAGE_PROVIDER
            if self.source == USAGE_PROVIDER and other.source == USAGE_PROVIDER
            else USAGE_ESTIMATE
        )
        return LLMUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            source=source,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total,
            "source": self.source,
            "estimated": self.estimated,
        }


class LLMClient(Protocol):
    """LLM 客户端协议。

    ``take_usage()`` **刻意不是**协议成员 —— 它是可选的旁路。要求所有实现都提供它，
    意味着每个测试替身都得跟着改；而拿不到用量本来就有兜底（字符估算），
    没必要为此把接口做硬。
    """

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


def take_usage(llm: Any) -> Optional[LLMUsage]:
    """取走客户端自上次取走以来**累计**的用量；拿不到就返回 ``None``。

    累计而非「仅上一次」：一次业务请求往往要调好几次模型（改写、重排、生成），
    只留最后一次会让成本被低估好几倍 —— 而低估比缺失更危险：
    缺失看得见，低估看不见。

    用 ``getattr`` 探测而不是要求协议成员：测试里的假 LLM 只实现 ``complete``，
    强行要求会让「加一个成本指标」变成「改几十个测试替身」。拿不到时调用方
    退回字符估算，行为不变。

    **取走语义（drain）**：读过一次就清空，避免同一次调用的用量被重复计入。
    """
    getter = getattr(llm, "take_usage", None)
    if getter is None or not callable(getter):
        return None
    try:
        usage = getter()
    except Exception:  # noqa: BLE001 - 用量是旁路信息，不能因为它让主链路失败
        logger.debug("take_usage 调用失败，退回字符估算", exc_info=True)
        return None
    return usage if isinstance(usage, LLMUsage) else None


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

    def take_usage(self) -> LLMUsage:
        """没有模型就没有消耗，真实值就是 0 —— 不是「拿不到」，是「确定为零」。"""
        return LLMUsage(source=USAGE_PROVIDER)


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
    _pending_usage: LLMUsage | None = field(default=None, init=False, repr=False)

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return f"llm:{self.provider}:{self.model}"

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def take_usage(self) -> Optional[LLMUsage]:
        """取走上一次取走以来累计的用量；provider 全程没回 usage 时返回 ``None``。"""
        usage, self._pending_usage = self._pending_usage, None
        return usage

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

    @staticmethod
    def _extract_usage(provider: str, body: dict[str, Any]) -> LLMUsage | None:
        """从响应体里取真实用量。取不到返回 ``None``（由调用方退回估算）。

        两种协议的字段名不一样，这里显式分开写而不是「猜一个」：
        猜错的话会静默产出 0，而 0 看起来像「这次调用不花钱」，比报错更误导。
        """
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None
        if provider == "anthropic":
            prompt = usage.get("input_tokens")
            completion = usage.get("output_tokens")
        else:
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")

        def _as_int(value: Any) -> int | None:
            if isinstance(value, bool) or value is None:
                return None
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed >= 0 else None

        prompt_tokens = _as_int(prompt)
        completion_tokens = _as_int(completion)
        if prompt_tokens is None and completion_tokens is None:
            return None
        return LLMUsage(
            prompt_tokens=prompt_tokens or 0,
            completion_tokens=completion_tokens or 0,
            source=USAGE_PROVIDER,
        )

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
        # 只有拿到文本才算这次调用有效，此刻才把用量挂上去。
        # 累加而不是覆盖：一个请求链路（改写 + 重排 + 生成）可能调好几次，
        # 只留最后一次会让成本被严重低估。
        usage = self._extract_usage(self.provider, body)
        if usage is not None:
            self._pending_usage = usage if self._pending_usage is None else self._pending_usage + usage
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
