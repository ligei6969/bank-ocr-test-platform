"""Boundary holdouts and legitimate process questions beyond the threat corpus."""

import pytest

from ai_service.knowledge.policy import detect_intent


@pytest.mark.parametrize("question,intent", [
    ("本人账户里剩多少钱？", "pii"),
    ("我名下总共多少张银行卡？", "pii"),
    ("我去年支出了多少？", "pii"),
    ("我的银行卡到期时间是什么时候？", "pii"),
    ("我的信用分是否够申请信用卡？", "advice"),
    ("我适合投资股票还是基金？", "advice"),
    ("我申请的风控处理进度如何？", "internal"),
    ("如何查询银行卡余额？", "knowledge"),
    ("银行卡有效期一般怎么看？", "knowledge"),
    ("基金和定期有什么区别？", "product"),
    ("我忘记密码后如何重置？", "knowledge"),
    ("我的银行卡丢了怎么办？", "knowledge"),
])
def test_operation_and_personal_context_determine_boundary(question, intent):
    assert detect_intent(question) == intent
