"""结构化输出：把模型返回的文本解析成经过校验的对象。

要解决的问题
------------
P0 里解析模型输出用的是 ``raw.find("[")`` / ``raw.rfind("]")`` 这种就地切片，
然后 ``json.loads``。能跑，但有三类输入会让它失效，而这三类在真实模型上都很常见：

* 模型套了 Markdown 代码围栏（``​```json ... ```​``）；
* 模型在 JSON 前后加了「好的，以下是排序结果：」这类寒暄；
* 模型返回了结构正确但语义非法的内容（索引越界、字段缺失、类型不对）。

前两类是格式清洗问题，第三类必须用 schema 校验兜住 —— 光靠 ``json.loads``
只能保证「是合法 JSON」，保证不了「是我们想要的东西」。

设计取舍
--------
**解析失败一律抛 :class:`StructuredOutputError`，绝不返回 None 让调用方自己猜。**
调用方（检索改写、重排、Agent 决策）都必须显式决定「失败后走哪条降级路径」。
静默返回 None 会让降级被吞掉 —— P0 已经吃过这个亏，``rewrite_query`` 就是靠
显式异常才把「LLM 失败 → 规则扩展」这条路径做得可观测。

超长输出直接拒绝而不是截断。截断 JSON 必然解析失败，与其抛一个令人困惑的
``JSONDecodeError``，不如明确告知「输出超长」—— 这个信号本身有价值
（说明模型跑偏了或 prompt 有问题）。
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

#: 单次模型输出的字符上限。正常输出在几百到几千字符，超过这个量级说明模型跑偏了。
MAX_RAW_CHARS = 65536

_FENCE_PATTERN = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)


class StructuredOutputError(ValueError):
    """模型输出无法解析为期望结构。调用方应据此走降级路径。"""


def _clean(raw: str) -> str:
    if raw is None:
        raise StructuredOutputError("模型返回空响应（None）")
    if not isinstance(raw, str):
        raw = str(raw)
    text = raw.strip()
    if not text:
        raise StructuredOutputError("模型返回空响应")
    if len(text) > MAX_RAW_CHARS:
        raise StructuredOutputError(
            f"模型输出超长：{len(text)} 字符 > 上限 {MAX_RAW_CHARS}"
        )
    return text


def _strip_fence(text: str) -> str:
    """剥掉 Markdown 代码围栏；没有围栏就原样返回。"""
    match = _FENCE_PATTERN.search(text)
    return match.group(1).strip() if match else text


def _slice_outermost(text: str, opener: str, closer: str) -> str:
    """从首个 ``opener`` 切到最后一个 ``closer``。

    这里刻意用「首个开 → 最后一个闭」的贪心切法，而不是做括号配平：
    期望的响应里只有一个 JSON 值，贪心最多把尾随文本一起带进来，
    而这会被随后的 ``json.loads`` 拒掉并给出明确错误。
    真要配平需要处理字符串内的引号转义，复杂度远大于收益。
    """
    start = text.find(opener)
    end = text.rfind(closer)
    if start < 0 or end < 0 or end <= start:
        raise StructuredOutputError(f"模型响应里找不到成对的 {opener}{closer}")
    return text[start : end + 1]


def extract_json_text(raw: str, *, expect: str = "object") -> str:
    """从模型输出里抽出 JSON 文本片段。``expect`` 取 ``object`` 或 ``array``。"""
    text = _strip_fence(_clean(raw))
    if expect == "array":
        return _slice_outermost(text, "[", "]")
    return _slice_outermost(text, "{", "}")


def _loads(candidate: str) -> Any:
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"JSON 解析失败：{exc.msg}（位置 {exc.pos}）") from exc


def parse_structured(raw: str, model: Type[T]) -> T:
    """把模型输出解析成 ``model`` 实例；任何不合规都抛 :class:`StructuredOutputError`。"""
    payload = _loads(extract_json_text(raw, expect="object"))
    if not isinstance(payload, dict):
        raise StructuredOutputError(f"期望 JSON 对象，实际是 {type(payload).__name__}")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise StructuredOutputError(
            f"结构校验失败（{exc.error_count()} 处）：{_summarize_errors(exc)}"
        ) from exc


def parse_json_array(
    raw: str,
    *,
    item_type: Optional[Type[Any]] = None,
    allow_empty: bool = False,
) -> List[Any]:
    """解析 JSON 数组，可选校验元素类型。用于查询改写与重排。

    ``allow_empty=False`` 时空数组视为失败 —— 对改写/重排来说，
    空结果等价于「没干活」，应该走降级而不是把空列表当成正常结果往下传。
    """
    payload = _loads(extract_json_text(raw, expect="array"))
    if not isinstance(payload, list):
        raise StructuredOutputError(f"期望 JSON 数组，实际是 {type(payload).__name__}")
    if not payload and not allow_empty:
        raise StructuredOutputError("模型返回空数组")
    if item_type is not None:
        for index, item in enumerate(payload):
            if not isinstance(item, item_type):
                raise StructuredOutputError(
                    f"数组第 {index} 项类型错误：期望 {item_type.__name__}，"
                    f"实际 {type(item).__name__}"
                )
    return payload


def parse_string_array(raw: str, *, allow_empty: bool = False) -> List[str]:
    """字符串数组的便捷入口，顺带剔除空白项。"""
    items = parse_json_array(raw, item_type=str, allow_empty=allow_empty)
    cleaned = [item.strip() for item in items if item.strip()]
    if not cleaned and not allow_empty:
        raise StructuredOutputError("数组里没有非空字符串")
    return cleaned


def parse_index_array(raw: str, *, upper_bound: int) -> List[int]:
    """解析重排用的索引数组，并剔除越界索引。

    越界索引不算致命错误（模型经常多给一个），静默丢弃即可；
    但**全部越界**说明模型根本没按索引作答，这时必须失败。
    """
    items = parse_json_array(raw, allow_empty=False)
    picked: List[int] = []
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            index = item
        elif isinstance(item, float) and item.is_integer():
            index = int(item)
        else:
            continue
        if 0 <= index < upper_bound and index not in picked:
            picked.append(index)
    if not picked:
        raise StructuredOutputError(
            f"索引数组全部越界或非法（共 {len(items)} 项，合法范围 0..{upper_bound - 1}）"
        )
    return picked


def _summarize_errors(exc: ValidationError, limit: int = 3) -> str:
    parts: List[str] = []
    for error in exc.errors()[:limit]:
        location = ".".join(str(item) for item in error.get("loc", ())) or "<root>"
        parts.append(f"{location}: {error.get('msg', '校验失败')}")
    return "；".join(parts)


async def complete_structured(
    llm: Any,
    prompt: str,
    model: Type[T],
    *,
    system: Optional[str] = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
) -> T:
    """调用模型并把结果解析成 ``model``。失败时抛 :class:`StructuredOutputError`。

    这里刻意**不吞异常也不重试**：重试属于调用方的策略（改写降级为规则、
    决策降级为固定序列，各有各的兜底），放在这一层会把策略藏起来。
    """
    raw = await llm.complete(
        prompt, system=system, max_tokens=max_tokens, temperature=temperature
    )
    return parse_structured(raw, model)


__all__ = (
    "MAX_RAW_CHARS",
    "StructuredOutputError",
    "complete_structured",
    "extract_json_text",
    "parse_index_array",
    "parse_json_array",
    "parse_string_array",
    "parse_structured",
)
