"""Tests for ID-card side detection and field parsing."""

from app.id_card_parser import detect_id_card_side, parse_id_card_fields


def test_detects_and_parses_id_card_front() -> None:
    text = "\n".join(
        [
            "姓名 李雷",
            "性别 男 民族 苗",
            "出生 1986年1月22日",
            "住址 安徽省月江市城东区文昌街64号",
            "公民身份号码 110101198601220011",
        ]
    )

    parsed = parse_id_card_fields(text)

    assert parsed["side"] == "front"
    assert parsed["fields"] == {
        "name": "李雷",
        "gender": "男",
        "nation": "苗",
        "birth": "1986-01-22",
        "address": "安徽省月江市城东区文昌街64号",
        "id_number": "110101198601220011",
    }


def test_detects_and_parses_id_card_back() -> None:
    text = "\n".join(
        [
            "中华人民共和国",
            "居民身份证",
            "签发机关 月江市公安局",
            "有效期限 2020.01.01-2040.01.01",
        ]
    )

    parsed = parse_id_card_fields(text)

    assert parsed["side"] == "back"
    assert parsed["fields"] == {
        "issue_authority": "月江市公安局",
        "valid_period": "2020.01.01-2040.01.01",
    }


def test_detects_unknown_id_card_side() -> None:
    assert detect_id_card_side("UNRELATED OCR TEXT") == "unknown"

