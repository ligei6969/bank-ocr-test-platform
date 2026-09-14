"""AI 复核助手的调用客户端。

设计立场
--------
**这个模块永远不会抛异常。** 所有调用路径都以 try/except 收口，
失败时返回结构化降级结果。原因是：AI 是增强能力，不是审核主链路的一环 ——
AI 服务挂了、超时了、返回垃圾了，审核记录页都必须照常可用。

三件套（对齐 EchoMind ``mcp/tool_manager.py`` 的做法）：

* **超时**：``AI_ASSIST_TIMEOUT_S`` 默认 3 秒。宁可没有 AI 解释，也不让页面转圈。
* **熔断**：连续 ``AI_ASSIST_FAILURE_THRESHOLD`` 次失败后打开电路，
  ``AI_ASSIST_RECOVERY_S`` 秒内直接返回降级结果，不再发请求。
  这样 AI 服务长时间宕机时，页面不会每次都白等一个超时。
* **降级**：返回统一结构的 ``available=False`` 结果，前端能明确区分
  「AI 说这条没问题」和「AI 没回答」。

安全边界
--------
**脱敏由本模块强制执行**，不依赖调用方记得做。请求发出前会对
``fields``、``error_message``、``question`` 统一执行 ``sanitize_for_log``，
确保证件号与姓名不会离开本机边界。这一点和平台日志脱敏用的是同一套函数。

配置（环境变量）
----------------
=======================  ==========================================  =============
变量                      含义                                        默认值
=======================  ==========================================  =============
AI_ASSIST_ENABLED        总开关；false 时完全不发请求                true
AI_SERVICE_URL           AI 服务地址                                 http://127.0.0.1:8100
AI_ASSIST_TIMEOUT_S      单次调用超时（秒）                          3.0
AI_ASSIST_FAILURE_THRESHOLD  连续失败几次后熔断                        3
AI_ASSIST_RECOVERY_S     熔断后多少秒进入半开探测                     60
=======================  ==========================================  =============
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from app.logging_utils import sanitize_for_log, sanitize_review_fields

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_URL = "http://127.0.0.1:8100"
DEFAULT_TIMEOUT_S = 3.0
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_RECOVERY_S = 60.0
MAX_RESPONSE_BYTES = 1_000_000
TRUTHY = {"1", "true", "yes", "on", "y", "t"}

DEGRADED_MESSAGES = {
    "disabled": "AI 复核助手未启用（AI_ASSIST_ENABLED=false），本页仅展示原始原因码。",
    "circuit_open": "AI 复核服务连续失败已熔断，稍后会自动重试；本页暂展示原始原因码。",
    "timeout": "AI 复核服务响应超时，本页暂展示原始原因码。",
    "unreachable": "AI 复核服务未启动或网络不可达，本页暂展示原始原因码。",
    "invalid_response": "AI 复核服务返回了无法解析的内容，本页暂展示原始原因码。",
    "http_error": "AI 复核服务返回错误状态码，本页暂展示原始原因码。",
}


# ── 熔断器（与 ai_service/tool_manager.py 同构）────────────────────────────────

class CircuitState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _CircuitBreaker:
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    recovery_s: float = DEFAULT_RECOVERY_S
    state: str = CircuitState.CLOSED
    fail_count: int = 0
    opened_at: Optional[float] = None

    def allow(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self.opened_at is not None and time.monotonic() - self.opened_at >= self.recovery_s:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True  # HALF_OPEN：放行一次探测

    def record_success(self) -> None:
        self.fail_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.fail_count += 1
        if self.fail_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = time.monotonic()
            logger.warning("AI 复核客户端熔断打开（连续失败 %s 次）", self.fail_count)


# ── 客户端 ────────────────────────────────────────────────────────────────────

@dataclass
class AIAssistClient:
    """平台访问 AI 复核服务的唯一入口。"""

    base_url: str = DEFAULT_SERVICE_URL
    enabled: bool = True
    timeout_s: float = DEFAULT_TIMEOUT_S
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    recovery_s: float = DEFAULT_RECOVERY_S
    _breaker: _CircuitBreaker = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False, default_factory=threading.Lock)
    _last_error: Optional[str] = field(init=False, default=None, repr=False)
    _call_count: int = field(init=False, default=0, repr=False)
    _failure_count: int = field(init=False, default=0, repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._breaker = _CircuitBreaker(
            failure_threshold=self.failure_threshold,
            recovery_s=self.recovery_s,
        )

    # ── 状态 ──────────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "base_url": self.base_url,
                "timeout_s": self.timeout_s,
                "circuit_state": self._breaker.state,
                "consecutive_failures": self._breaker.fail_count,
                "failure_threshold": self._breaker.failure_threshold,
                "call_count": self._call_count,
                "failure_count": self._failure_count,
                "last_error": self._last_error,
            }

    def reset(self) -> None:
        """清空熔断与统计状态。测试与运维排查用。"""
        with self._lock:
            self._breaker = _CircuitBreaker(
                failure_threshold=self.failure_threshold,
                recovery_s=self.recovery_s,
            )
            self._last_error = None
            self._call_count = 0
            self._failure_count = 0

    # ── 对外接口 ──────────────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """探测 AI 服务。永不抛异常。"""
        if not self.enabled:
            return {"available": False, "reason": "disabled"}
        payload, reason = self._request("GET", "/health", None)
        if payload is None:
            return {"available": False, "reason": reason}
        return {"available": True, **payload}

    def explain(self, payload: dict[str, Any]) -> dict[str, Any]:
        """请求一条审核记录的解释。**永不抛异常。**

        返回结构保证包含 ``available``、``degraded``、``reason_details``、
        ``actions``、``citations`` 等键，前端无需处理缺键情况。
        """
        request_id = str(payload.get("request_id", ""))
        if not self.enabled:
            return self._degraded(request_id, "disabled")

        body = self._sanitize_payload(payload)
        response, reason = self._request("POST", "/explain", body)
        if response is None:
            return self._degraded(request_id, reason)

        return self._normalize(response, request_id)

    # ── 脱敏 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """出站前统一脱敏，调用方不需要记得做这件事。

        ``fields`` 走 :func:`sanitize_review_fields`：卡号与身份证号按数字规则打星，
        姓名与住址另按文本规则保留首字。``question`` 与 ``error_message`` 也可能
        被粘贴进证件号，同样过一遍通用脱敏。
        """
        sanitized = dict(payload)
        if "fields" in sanitized:
            sanitized["fields"] = sanitize_review_fields(sanitized["fields"])
        for key in ("error_message", "question", "filename"):
            if key in sanitized:
                sanitized[key] = sanitize_for_log(sanitized[key])
        return sanitized

    # ── 传输 ──────────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]],
    ) -> tuple[Optional[dict[str, Any]], str]:
        """发一次 HTTP 请求。返回 ``(响应体, 失败原因)``，失败时响应体为 None。"""
        if not self._acquire_permission():
            return None, "circuit_open"

        url = f"{self.base_url}{path}"
        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        )
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method=method,
        )

        with self._lock:
            self._call_count += 1

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read(MAX_RESPONSE_BYTES)
        except TimeoutError:
            return None, self._record_failure("timeout", f"timeout after {self.timeout_s}s")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="ignore")[:200]
            except Exception:  # noqa: BLE001 - 读错误体失败不影响主流程
                detail = ""
            return None, self._record_failure("http_error", f"HTTP {exc.code} {detail}".strip())
        except urllib.error.URLError as exc:
            return None, self._record_failure("unreachable", str(exc.reason))
        except OSError as exc:
            # 连接被拒、DNS 失败等都会走到这里
            return None, self._record_failure("unreachable", str(exc))

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, self._record_failure("invalid_response", str(exc))

        if not isinstance(payload, dict):
            return None, self._record_failure("invalid_response", "response is not an object")

        with self._lock:
            self._breaker.record_success()
            self._last_error = None
        return payload, ""

    def _acquire_permission(self) -> bool:
        with self._lock:
            return self._breaker.allow()

    def _record_failure(self, reason: str, detail: str) -> str:
        with self._lock:
            self._breaker.record_failure()
            self._failure_count += 1
            self._last_error = f"{reason}: {detail}"
        logger.warning("AI 复核服务调用失败 reason=%s detail=%s", reason, detail)
        return reason

    # ── 结果规整 ──────────────────────────────────────────────────────────────

    @classmethod
    def _normalize(cls, response: dict[str, Any], request_id: str) -> dict[str, Any]:
        """把 AI 服务的响应规整成前端可直接消费的稳定结构。"""
        normalized = dict(response)
        normalized.setdefault("request_id", request_id)
        normalized["available"] = True
        normalized.setdefault("degraded", False)
        normalized.setdefault("explanation", "")
        for key in ("reason_details", "actions", "citations", "unknown_reason_codes", "trace"):
            value = normalized.get(key)
            if not isinstance(value, list):
                normalized[key] = []
        engine = normalized.get("engine")
        normalized["engine"] = engine if isinstance(engine, dict) else {}
        try:
            normalized["confidence"] = float(normalized.get("confidence") or 0.0)
        except (TypeError, ValueError):
            normalized["confidence"] = 0.0
        return normalized

    @classmethod
    def _degraded(cls, request_id: str, reason: str) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "available": False,
            "degraded": True,
            "reason": reason,
            "message": DEGRADED_MESSAGES.get(reason, DEGRADED_MESSAGES["unreachable"]),
            "explanation": "",
            "reason_details": [],
            "actions": [],
            "citations": [],
            "unknown_reason_codes": [],
            "confidence": 0.0,
            "engine": {"llm": "none", "llm_available": False, "generation": "unavailable"},
            "latency_ms": 0.0,
            "trace": [],
        }


# ── 配置与单例 ────────────────────────────────────────────────────────────────

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in TRUTHY


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def build_ai_client() -> AIAssistClient:
    """按环境变量构造客户端。每次调用都读一次环境，便于测试替换。"""
    return AIAssistClient(
        base_url=os.getenv("AI_SERVICE_URL", DEFAULT_SERVICE_URL).strip() or DEFAULT_SERVICE_URL,
        enabled=_env_bool("AI_ASSIST_ENABLED", True),
        timeout_s=_env_float("AI_ASSIST_TIMEOUT_S", DEFAULT_TIMEOUT_S),
        failure_threshold=max(1, _env_int("AI_ASSIST_FAILURE_THRESHOLD", DEFAULT_FAILURE_THRESHOLD)),
        recovery_s=_env_float("AI_ASSIST_RECOVERY_S", DEFAULT_RECOVERY_S),
    )


_client: Optional[AIAssistClient] = None
_client_lock = threading.Lock()


def get_ai_client() -> AIAssistClient:
    """返回进程内共享的客户端，保证熔断状态跨请求生效。"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = build_ai_client()
    return _client


def set_ai_client(client: Optional[AIAssistClient]) -> None:
    """替换单例。测试与热更新配置用。"""
    global _client
    with _client_lock:
        _client = client
