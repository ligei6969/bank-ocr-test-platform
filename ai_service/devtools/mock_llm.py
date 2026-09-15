"""协议级假模型：一个说 OpenAI / Anthropic 协议的本地 HTTP 服务。

它解决什么问题
--------------
P1 的测试用「进程内假 LLM」覆盖了 Agent 的逻辑，但有一条链从来没人验证过：
**HTTP 传输、鉴权头、请求体构造、响应解析、usage 抽取**。
这段代码只在连真实 provider 时才会跑到，而本机当时没有 key ——
于是它成了「最容易被面试官追问、又恰恰没验证」的一环。

这份靶子把这条链在**不联网、不花钱、无 key** 的前提下跑通：
它按真实协议返回，包括 ``usage`` 字段，所以可以在 CI 里验证
「用量是不是真的被解析了」「鉴权头是不是真的发出去了」。

它不是模型
----------
``demo_responder()`` 只按 prompt 里的关键词回一个**结构合法**的答案，
内容对不对它不管。所以：

* 它能证明「协议通、链路通、用量能取到」；
* 它**不能**证明「模型答得好」—— 那必须用真实模型，见 ``--live``。

把这两件事混为一谈，就是拿靶子的成绩当自己的成绩。
"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

DEFAULT_MODEL = "mock-provider-model"
DEFAULT_API_KEY = "mock-api-key"

#: 真实 provider 的 usage 是按 tokenizer 算的；这里不需要 tokenizer，
#: 只要「随长度单调增长且确定」，就够验证解析链路了。
CHARS_PER_TOKEN = 4


def pseudo_tokens(text: str) -> int:
    """确定性的假 token 数。**不是分词器**，只为让断言可复现。"""
    return max(1, len(text) // CHARS_PER_TOKEN)


def demo_responder() -> Callable[[str], str]:
    """默认响应策略：按 prompt 关键词判断该回哪种结构。

    状态只有一处：Agent 的决策序列要「先查再答」，否则第二轮就会撞上
    Agent 的「同一调用重复即收敛」保护。这属于靶子自己的事，
    与 Agent 的语义无关。
    """
    decisions = {"count": 0}

    def respond(prompt: str) -> str:
        if "请决定下一步" in prompt:
            decisions["count"] += 1
            if decisions["count"] == 1:
                return json.dumps(
                    {
                        "thought": "先查知识库确认原因码含义",
                        "action": "search_knowledge",
                        "params": {"query": "影像质量 原因码 处置建议", "top_k": 5},
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "thought": "证据已足够，收敛",
                    "action": "finish",
                    "params": {},
                    "answer": (
                        "这是本地假模型生成的解释正文，用于验证 HTTP 协议链路与"
                        "用量解析是否连通。它不具备模型能力，因此内容本身不构成"
                        "对审核结论的判断，仅作协议级冒烟之用。"
                    ),
                },
                ensure_ascii=False,
            )
        if "改写为" in prompt:
            return json.dumps(
                ["原因码含义", "处置建议", "拍摄规范"], ensure_ascii=False
            )
        if "排序" in prompt:
            return json.dumps([0, 1, 2, 3, 4])
        if "打 0~5 分" in prompt:
            return json.dumps(
                {
                    "relevance": 4,
                    "accuracy": 4,
                    "completeness": 4,
                    "usefulness": 4,
                    "rationale": "本地靶子的固定打分，无评价意义",
                },
                ensure_ascii=False,
            )
        return "这是本地假模型生成的解释正文，仅用于验证协议链路是否连通。"

    return respond


@dataclass
class MockLLMConfig:
    """靶子行为配置。测试用哪种故障，就设哪个字段。"""

    model: str = DEFAULT_MODEL
    #: 是否校验鉴权头。开着才能证明「客户端真的把 key 发出去了」
    require_auth: bool = True
    #: 响应体里不带 usage —— 用来验证客户端会退回字符估算而不是编个 0
    omit_usage: bool = False
    #: 前 N 个请求返回 500，用于验证客户端错误路径
    fail_first_n: int = 0
    #: 固定 token 数（不设则按长度推算），方便断言精确值
    fixed_prompt_tokens: Optional[int] = None
    fixed_completion_tokens: Optional[int] = None
    responder: Optional[Callable[[str], str]] = None
    #: 记录所有收到的请求，供测试断言「请求体长什么样」
    captured: List[Dict[str, Any]] = field(default_factory=list)


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockLLM/1.0"
    protocol_version = "HTTP/1.1"

    # 关掉 stderr 的访问日志，测试输出才干净
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    @property
    def mock(self) -> "MockLLMServer":
        return self.server.mock  # type: ignore[attr-defined]

    # ── 路由 ──────────────────────────────────────────────────────────────────

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        config = self.mock.config
        body = self._read_json()
        self.mock.record(path=self.path, body=body, headers=dict(self.headers))

        if config.fail_first_n > 0 and self.mock.request_count <= config.fail_first_n:
            self._send_json(500, {"error": {"message": "mock injected failure"}})
            return

        anthropic = "/messages" in self.path
        if not self._authorized(anthropic):
            self.mock.auth_failures += 1
            self._send_json(
                401,
                {"error": {"message": "missing or invalid credentials", "type": "auth"}},
            )
            return

        prompt = self._extract_prompt(anthropic, body)
        text = self.mock.responder(prompt)

        if anthropic:
            payload = {
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "model": config.model,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
            }
            if not config.omit_usage:
                payload["usage"] = {
                    "input_tokens": self._prompt_tokens(prompt),
                    "output_tokens": self._completion_tokens(text),
                }
        else:
            payload = {
                "id": "chatcmpl_mock",
                "object": "chat.completion",
                "model": config.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
            }
            if not config.omit_usage:
                payload["usage"] = {
                    "prompt_tokens": self._prompt_tokens(prompt),
                    "completion_tokens": self._completion_tokens(text),
                    "total_tokens": self._prompt_tokens(prompt) + self._completion_tokens(text),
                }

        self._send_json(200, payload)

    # ── 工具 ──────────────────────────────────────────────────────────────────

    def _prompt_tokens(self, prompt: str) -> int:
        override = self.mock.config.fixed_prompt_tokens
        return override if override is not None else pseudo_tokens(prompt)

    def _completion_tokens(self, text: str) -> int:
        override = self.mock.config.fixed_completion_tokens
        return override if override is not None else pseudo_tokens(text)

    def _authorized(self, anthropic: bool) -> bool:
        if not self.mock.config.require_auth:
            return True
        if anthropic:
            return bool(self.headers.get("x-api-key"))
        return bool(self.headers.get("Authorization"))

    @staticmethod
    def _extract_prompt(anthropic: bool, body: Dict[str, Any]) -> str:
        if anthropic:
            blocks = body.get("messages") or []
        else:
            blocks = body.get("messages") or []
        parts: List[str] = []
        system = body.get("system")
        if isinstance(system, str) and system:
            parts.append(system)
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("content"), str):
                parts.append(block["content"])
        return "\n".join(parts)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MockLLMServer:
    """可嵌入测试的靶子服务。

    用法::

        with MockLLMServer() as server:
            client = build_llm_client(server.openai_env())
    """

    def __init__(self, config: Optional[MockLLMConfig] = None) -> None:
        self.config = config or MockLLMConfig()
        # 默认响应器**整个服务只建一次**。它内部有「先查再答」的计数状态，
        # 若放在请求处理里现建，计数每次归零，Agent 会一直收到同一个决策，
        # 最后撞上「重复调用即收敛」而提前结束 —— 靶子自己制造出一个假故障。
        if self.config.responder is None:
            self.config.responder = demo_responder()
        self.request_count = 0
        self.auth_failures = 0
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = 0

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def start(self) -> "MockLLMServer":
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.daemon_threads = True
        httpd.mock = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None

    def __enter__(self) -> "MockLLMServer":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    # ── 记账 ──────────────────────────────────────────────────────────────────

    @property
    def responder(self) -> Callable[[str], str]:
        """当前生效的响应器。``__init__`` 保证它一定存在。"""
        if self.config.responder is None:  # pragma: no cover - 兜底，正常不会走到
            self.config.responder = demo_responder()
        return self.config.responder

    def record(self, *, path: str, body: Dict[str, Any], headers: Dict[str, str]) -> None:
        with self._lock:
            self.request_count += 1
            self.requests.append({"path": path, "body": body, "headers": headers})

    # ── 客户端配置 ────────────────────────────────────────────────────────────

    @property
    def base_url_openai(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def base_url_anthropic(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def openai_env(self, api_key: str = DEFAULT_API_KEY) -> Dict[str, str]:
        """``build_llm_client`` 能直接吃的一组环境变量。"""
        return {
            "LLM_PROVIDER": "openai",
            "LLM_API_KEY": api_key,
            "LLM_BASE_URL": self.base_url_openai,
            "LLM_MODEL": self.config.model,
            "LLM_TIMEOUT_S": "10",
            "LLM_MAX_RETRIES": "0",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    def anthropic_env(self, api_key: str = DEFAULT_API_KEY) -> Dict[str, str]:
        return {
            "LLM_PROVIDER": "anthropic",
            "LLM_API_KEY": api_key,
            "LLM_BASE_URL": self.base_url_anthropic,
            "LLM_MODEL": self.config.model,
            "LLM_TIMEOUT_S": "10",
            "LLM_MAX_RETRIES": "0",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_service.devtools.mock_llm",
        description="启动本地协议靶子，供 --live / cassette 录制做无 key 冒烟",
    )
    parser.add_argument("--port", type=int, default=8137, help="监听端口，默认 8137")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--no-auth", action="store_true", help="不校验鉴权头")
    parser.add_argument("--omit-usage", action="store_true", help="响应体不带 usage")
    args = parser.parse_args(argv)

    config = MockLLMConfig(require_auth=not args.no_auth, omit_usage=args.omit_usage)
    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    httpd.daemon_threads = True
    server = MockLLMServer(config)
    httpd.mock = server  # type: ignore[attr-defined]
    server._httpd = httpd  # noqa: SLF001 - CLI 场景下直接接管

    print(f"协议靶子已启动：http://{args.host}:{args.port}", flush=True)
    print("OpenAI 兼容：LLM_BASE_URL=" + f"http://{args.host}:{args.port}/v1", flush=True)
    print("Anthropic  ：LLM_BASE_URL=" + f"http://{args.host}:{args.port}", flush=True)
    print(f"鉴权头：{'校验' if config.require_auth else '不校验'}；usage：{'不带' if config.omit_usage else '带'}", flush=True)
    print("", flush=True)
    print("配合使用（另开一个终端）：", flush=True)
    print(
        f"  LLM_PROVIDER=openai LLM_API_KEY=dev LLM_BASE_URL=http://{args.host}:{args.port}/v1 "
        "python -m ai_service --agent --live",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "DEFAULT_API_KEY",
    "CHARS_PER_TOKEN",
    "MockLLMConfig",
    "MockLLMServer",
    "demo_responder",
    "main",
    "pseudo_tokens",
)
