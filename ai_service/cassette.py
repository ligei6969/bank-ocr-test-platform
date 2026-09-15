"""LLM 与工具调用的 record / replay（cassette）代理。

要解决的问题
------------
Agent 是不确定系统：同一个输入，模型可能给出不同轨迹。把它放进 CI 有两个后果 ——
**抖动**（今天过明天红）和**烧钱**（每次跑都真调模型）。常见的「跳过这些测试」
是逃避，不是解决。

这里的做法是 cassette（录音带）模式，和 VCR / Polly.js 一个思路：
真实跑一次并把每次调用录下来，之后 CI 只回放，永不联网。

两种模式的契约
--------------
``live``
    真实调用，并把「请求指纹 → 响应」写进 cassette。只有手动跑才会用。

``replay``
    只读 cassette。**命中就返回录制内容，未命中直接抛
    :class:`CassetteMissError`，绝不回退到真实调用。** 这条是硬约束：
    如果 replay 在未命中时「顺手」去联网，那 CI 就重新变成不确定的了，
    cassette 也就失去意义 —— 一个悄悄降级的回放层比没有回放层更危险。

为什么用「请求指纹」而不是顺序匹配
----------------------------------
顺序匹配（第 N 次调用对应第 N 条录制）在 Agent 场景很脆弱：模型多调一次工具，
后面全部错位，报出来的错还很难懂。按「规范化后的请求内容」做哈希，则
「同一个问题命中同一条录制」，与调用顺序无关，错位问题自然消失。

代价是**同一个请求的不同次响应无法区分**（比如两次调用同一 prompt 但温度不同）。
本服务的调用都固定在 ``temperature=0``，因此这个代价不成立。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ai_service.llm import (
    USAGE_PROVIDER,
    LLMClient,
    LLMUnavailableError,
    LLMUsage,
    take_usage,
)
from ai_service.tool_manager import Tool

logger = logging.getLogger(__name__)

CASSETTE_VERSION = 1
LIVE = "live"
REPLAY = "replay"
VALID_MODES = (LIVE, REPLAY)


class CassetteMissError(BaseException):
    """replay 模式下没有对应录制。

    为什么继承 ``BaseException`` 而不是 ``Exception``
    ------------------------------------------------
    这是**测试基础设施错误**，不是业务异常。Agent 内部有一圈
    ``except Exception``（工具层吞掉所有异常、``ToolManager`` 把失败包成
    ``ToolResult``），如果这个错误继承 ``Exception``，它会被安静地吃掉：
    Agent 退化成「工具坏了」继续跑完，测试**绿着骗人**。

    所以它刻意派生自 ``BaseException`` —— 和 ``KeyboardInterrupt``、
    ``SystemExit`` 同一个思路：「这不是正常业务流程里该被兜住的东西」。
    效果是无论隔了多少层 ``except Exception``，录制缺失都会一路炸到测试框架。
    """

    def __init__(self, message: str, *, kind: str = "", key: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.key = key


def fingerprint(kind: str, payload: Mapping[str, Any]) -> str:
    """由调用内容算出稳定指纹。

    用 ``sort_keys`` + 不保留空格，保证同一份内容永远得到同一个键 ——
    dict 顺序或缩进变化不该产生新键，否则 cassette 会天天失效。
    """
    canonical = json.dumps(
        {"kind": kind, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{kind}:{digest[:32]}"


@dataclass
class Cassette:
    """一份录制内容。结构刻意保持扁平，方便人工审阅与手动编辑。"""

    path: Optional[Path] = None
    mode: str = REPLAY
    entries: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0
    recorded: int = 0

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"cassette 模式必须是 {VALID_MODES}，得到 {self.mode!r}")

    # ── 读写 ──────────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path, mode: str) -> "Cassette":
        """从磁盘加载。replay 模式下文件不存在不是错误 —— 只是所有调用都会未命中。"""
        cassette = cls(path=Path(path), mode=mode)
        if Path(path).is_file():
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            version = raw.get("version")
            if version != CASSETTE_VERSION:
                raise ValueError(
                    f"cassette 版本不匹配：文件是 {version}，当前支持 {CASSETTE_VERSION}"
                )
            cassette.entries = dict(raw.get("entries") or {})
        elif mode == REPLAY:
            logger.warning("cassette 不存在，replay 模式将全部未命中: %s", path)
        return cassette

    def save(self) -> None:
        """落盘。live 模式收尾时调用。"""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"version": CASSETTE_VERSION, "entries": self.entries},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    # ── 交互 ──────────────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self.entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, key: str, entry: Dict[str, Any]) -> None:
        self.entries[key] = entry
        self.recorded += 1

    def stats(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "path": str(self.path) if self.path else None,
            "entries": len(self.entries),
            "hits": self.hits,
            "misses": self.misses,
            "recorded": self.recorded,
        }


# ── LLM 代理 ──────────────────────────────────────────────────────────────────

@dataclass
class CassetteLLMClient:
    """包一层 LLM 客户端，把「prompt → 文本」录下来。

    不改变 ``available`` / ``name`` 语义：上层照常判断模型可不可用，
    只是调用被中转了一层。

    **用量一并录制。** 否则回放时 token 记账会从「真实用量」掉回「字符估算」，
    同一份录制在 CI 里报出的成本与录制时不一致 —— 这种不一致很容易被误读成
    「模型换了」或者「成本涨了」。
    """

    inner: LLMClient
    cassette: Cassette
    _pending_usage: Optional[LLMUsage] = field(default=None, init=False, repr=False)

    @property
    def available(self) -> bool:
        """replay 模式下恒为 True。

        语义是「**录制说了算**」：replay 时 cassette 就是模型。恒为 True 才能
        保证「cassette 缺失 → 未命中 → 抛错」，而不是因为没配 key 就悄悄
        降级成确定性序列 —— 后者会让 CI 绿着骗人，正是要防的事。

        确实想要「不调模型的降级路径」，用 ``mode=live`` 配 ``NullLLMClient``，
        或者干脆别包这一层。
        """
        if self.cassette.mode == REPLAY:
            return True
        return self.inner.available

    @property
    def name(self) -> str:
        return f"cassette:{self.cassette.mode}:{self.inner.name}"

    async def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        describe = {
            "prompt": prompt,
            "system": system or "",
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        key = fingerprint("llm", describe)

        if self.cassette.mode == REPLAY:
            entry = self.cassette.get(key)
            if entry is None:
                raise _miss("llm", key, describe)
            # 累加而不是覆盖：一次请求可能调好几次模型，只留最后一次会低估成本
            self._pending_usage = _accumulate(self._pending_usage, _usage_from_entry(entry))
            return entry.get("response")

        response = await self.inner.complete(
            prompt, system=system, max_tokens=max_tokens, temperature=temperature
        )
        usage = take_usage(self.inner)
        record: Dict[str, Any] = {
            "kind": "llm",
            "request": dict(describe),
            "response": response,
        }
        if usage is not None:
            record["usage"] = usage.as_dict()
        self.cassette.put(key, record)
        self._pending_usage = _accumulate(self._pending_usage, usage)
        return response

    def take_usage(self) -> Optional[LLMUsage]:
        """回放时返回录制里的用量，录制时返回内层客户端的真实用量。"""
        usage, self._pending_usage = self._pending_usage, None
        return usage


def _accumulate(current: Optional[LLMUsage], extra: Optional[LLMUsage]) -> Optional[LLMUsage]:
    """把新到的用量并进累计值；两边都为 ``None`` 时保持 ``None``。

    保持 ``None`` 很重要：它表示「拿不到真实用量」，上层据此退回字符估算。
    若在这里用 0 顶替，估算路径就永远不会被走到，成本会静默变成 0。
    """
    if extra is None:
        return current
    return extra if current is None else current + extra


def _usage_from_entry(entry: Mapping[str, Any]) -> Optional[LLMUsage]:
    """从录制条目里还原用量。

    旧版 cassette 没有 ``usage`` 字段，这里返回 ``None``，
    上层自然退回字符估算 —— 兼容旧录制，不必强制重录。
    """
    raw = entry.get("usage")
    if not isinstance(raw, Mapping):
        return None

    def _as_int(value: Any) -> Optional[int]:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value >= 0 else None

    prompt_tokens = _as_int(raw.get("prompt_tokens"))
    completion_tokens = _as_int(raw.get("completion_tokens"))
    if prompt_tokens is None or completion_tokens is None:
        return None
    source = str(raw.get("source") or USAGE_PROVIDER)
    return LLMUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        source=source,
    )


async def _atransact(
    cassette: Cassette,
    key: str,
    kind: str,
    describe: Mapping[str, Any],
    call: Any,
) -> Any:
    if cassette.mode == REPLAY:
        entry = cassette.get(key)
        if entry is None:
            raise CassetteMissError(
                f"replay 模式下没有录制，且禁止联网：kind={kind} key={key} "
                f"描述={json.dumps(describe, ensure_ascii=False, default=str)[:200]}",
                kind=kind,
                key=key,
            )
        return entry.get("response")

    response = await call()
    cassette.put(key, {"kind": kind, "request": dict(describe), "response": response})
    return response


def _miss(kind: str, key: str, describe: Mapping[str, Any]) -> CassetteMissError:
    return CassetteMissError(
        f"replay 模式下没有录制，且禁止联网：kind={kind} key={key} "
        f"描述={json.dumps(describe, ensure_ascii=False, default=str)[:200]}",
        kind=kind,
        key=key,
    )


# ── 工具代理 ──────────────────────────────────────────────────────────────────

def wrap_tool(tool: Tool, cassette: Cassette) -> Tool:
    """返回一个「录放版」工具，参数与结果都和原工具一致。

    只包 ``handler``：熔断、缓存、超时、schema 仍在 ``ToolManager`` 里，
    不重复实现一遍。录制层只关心「输入 → 输出」。
    """
    original = tool.handler

    def wrapped(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> Any:
        describe = {"tool": tool.name, "params": params}
        key = fingerprint("tool", describe)

        if cassette.mode == REPLAY:
            entry = cassette.get(key)
            if entry is None:
                raise _miss("tool", key, describe)
            return entry.get("response")

        result = original(params, context)
        if inspect.isawaitable(result):
            raise TypeError(
                "wrap_tool 只支持同步 handler；异步 handler 请用 wrap_async_handler"
            )
        cassette.put(key, {"kind": "tool", "request": describe, "response": result})
        return result

    tool.handler = wrapped
    return tool


def wrap_async_handler(name: str, handler: Any, cassette: Cassette) -> Any:
    """异步 handler 的录放包装。"""

    async def wrapped(params: Dict[str, Any], context: Optional[Dict[str, Any]]) -> Any:
        describe = {"tool": name, "params": params}
        key = fingerprint("tool", describe)

        async def call() -> Any:
            return await handler(params, context)

        return await _atransact(cassette, key, "tool", describe, call)

    return wrapped


def record_cassette(
    path: Path,
    llm: LLMClient,
    *,
    mode: str = LIVE,
) -> tuple[CassetteLLMClient, Cassette]:
    """便捷入口：构造一个录放版 LLM 客户端。

    ``mode=live`` 时调用方在跑完后要自己调用 ``cassette.save()``。
    """
    cassette = Cassette.load(Path(path), mode)
    return CassetteLLMClient(inner=llm, cassette=cassette), cassette


__all__ = (
    "CASSETTE_VERSION",
    "LIVE",
    "REPLAY",
    "VALID_MODES",
    "Cassette",
    "CassetteLLMClient",
    "CassetteMissError",
    "fingerprint",
    "record_cassette",
    "wrap_async_handler",
    "wrap_tool",
)
