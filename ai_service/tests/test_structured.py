"""结构化输出解析的测试。

这里测的不是「能不能解析合法 JSON」—— 那是 ``json.loads`` 的事。
真正要钉住的是**失败时的行为**：模型返回垃圾时必须抛可捕获的异常，
而不是崩溃、也不是静默返回 None 让调用方自己猜。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_service.structured import (
    MAX_RAW_CHARS,
    StructuredOutputError,
    complete_structured,
    extract_json_text,
    parse_index_array,
    parse_json_array,
    parse_string_array,
    parse_structured,
)


class Decision(BaseModel):
    """模拟 Agent 的决策对象，用来验证 schema 校验确实生效。"""

    action: str = Field(min_length=1)
    reason: str = ""
    confidence: float = 0.0


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class ScriptedLLM:
    """按脚本返回固定文本的假模型。"""

    def __init__(self, response: str) -> None:
        self.response = response

    @property
    def available(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "llm:scripted"

    async def complete(self, prompt: str, **_: Any) -> str:
        return self.response


# ── 正常路径 ──────────────────────────────────────────────────────────────────

def test_valid_json_is_parsed_into_the_model() -> None:
    decision = parse_structured(
        '{"action": "search_knowledge", "reason": "需要查释义", "confidence": 0.9}',
        Decision,
    )

    assert isinstance(decision, Decision)
    assert decision.action == "search_knowledge"
    assert decision.confidence == pytest.approx(0.9)


def test_json_wrapped_in_markdown_fence_is_parsed() -> None:
    raw = '好的，结果如下：\n```json\n{"action": "escalate_to_human"}\n```\n还需要我继续吗？'

    assert parse_structured(raw, Decision).action == "escalate_to_human"


def test_json_surrounded_by_prose_is_parsed() -> None:
    raw = '以下是决策：{"action": "finish", "reason": "证据已足够"} 以上。'

    assert parse_structured(raw, Decision).reason == "证据已足够"


def test_complete_structured_passes_the_prompt_through() -> None:
    llm = ScriptedLLM('{"action": "finish"}')

    decision = run(complete_structured(llm, "请决策", Decision))

    assert decision.action == "finish"


def test_string_array_drops_blank_items() -> None:
    assert parse_string_array('["图片模糊", "  ", "有效期缺失"]') == ["图片模糊", "有效期缺失"]


# ── 失败路径：必须抛可捕获异常，不能崩 ────────────────────────────────────────

def test_malformed_json_raises_a_catchable_error() -> None:
    with pytest.raises(StructuredOutputError) as excinfo:
        parse_structured('{"action": "search_knowledge",,}', Decision)

    assert "JSON 解析失败" in str(excinfo.value)
    # 必须是 ValueError 的子类，调用方可以用统一的方式兜住
    assert isinstance(excinfo.value, ValueError)


def test_empty_response_raises_instead_of_returning_none() -> None:
    for raw in ("", "   ", None):
        with pytest.raises(StructuredOutputError):
            parse_structured(raw, Decision)  # type: ignore[arg-type]


def test_response_without_any_json_object_raises() -> None:
    with pytest.raises(StructuredOutputError) as excinfo:
        parse_structured("模型今天不想干活。", Decision)

    assert "找不到成对的" in str(excinfo.value)


def test_array_response_where_object_expected_raises() -> None:
    with pytest.raises(StructuredOutputError):
        parse_structured('["action"]', Decision)


def test_schema_violation_reports_the_offending_field() -> None:
    with pytest.raises(StructuredOutputError) as excinfo:
        # action 缺失 + confidence 类型不对
        parse_structured('{"confidence": "很高"}', Decision)

    message = str(excinfo.value)
    assert "结构校验失败" in message
    assert "action" in message


def test_empty_string_where_min_length_one_required_raises() -> None:
    with pytest.raises(StructuredOutputError):
        parse_structured('{"action": ""}', Decision)


def test_oversized_output_is_rejected_with_a_clear_reason() -> None:
    raw = '{"action": "' + "x" * (MAX_RAW_CHARS + 1) + '"}'

    with pytest.raises(StructuredOutputError) as excinfo:
        parse_structured(raw, Decision)

    assert "超长" in str(excinfo.value)


def test_empty_array_is_rejected_by_default() -> None:
    with pytest.raises(StructuredOutputError) as excinfo:
        parse_string_array("[]")

    assert "空数组" in str(excinfo.value)


def test_empty_array_can_be_allowed_explicitly() -> None:
    assert parse_string_array("[]", allow_empty=True) == []


def test_wrong_item_type_in_array_raises() -> None:
    with pytest.raises(StructuredOutputError) as excinfo:
        parse_string_array('["ok", 42]')

    assert "类型错误" in str(excinfo.value)


# ── 索引数组 ──────────────────────────────────────────────────────────────────

def test_index_array_drops_out_of_range_entries() -> None:
    # 模型多给一个越界索引是常态，静默丢弃即可
    assert parse_index_array("[2, 0, 99, 1]", upper_bound=3) == [2, 0, 1]


def test_index_array_deduplicates() -> None:
    assert parse_index_array("[1, 1, 0]", upper_bound=2) == [1, 0]


def test_index_array_rejects_all_out_of_range() -> None:
    with pytest.raises(StructuredOutputError) as excinfo:
        parse_index_array("[7, 8]", upper_bound=2)

    assert "全部越界" in str(excinfo.value)


def test_index_array_ignores_booleans() -> None:
    # bool 是 int 的子类，不特判的话 True 会被当成索引 1
    assert parse_index_array("[true, 0]", upper_bound=2) == [0]


def test_index_array_accepts_integral_floats() -> None:
    assert parse_index_array("[1.0, 2.0]", upper_bound=3) == [1, 2]


# ── 抽取函数本身 ──────────────────────────────────────────────────────────────

def test_extract_json_text_handles_array_expectation() -> None:
    assert extract_json_text("前缀 [1, 2] 后缀", expect="array") == "[1, 2]"


def test_parse_json_array_accepts_objects() -> None:
    items = parse_json_array('[{"doc_id": "a"}, {"doc_id": "b"}]')

    assert [item["doc_id"] for item in items] == ["a", "b"]
