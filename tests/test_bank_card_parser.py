"""Tests for synthetic bank-card OCR field parsing."""

from app.field_parser import (
    extract_card_number,
    extract_cardholder_name,
    extract_valid_date,
    normalize_card_number,
    parse_bank_card_fields,
)


def test_extracts_continuous_16_digit_card_number() -> None:
    text = "TEST BANK\nCARD NUMBER 6222020202020001\nZHANG SAN"

    assert extract_card_number(text) == "6222020202020001"


def test_extracts_and_normalizes_spaced_card_number() -> None:
    text = "SYNTHETIC CARD\n6222 0202 0202 0001\nFOR TEST ONLY"

    assert extract_card_number(text) == "6222020202020001"


def test_extracts_hyphenated_19_digit_card_number() -> None:
    text = "BANK CARD\nCARD NO. 6222-0202-0202-0001-001\nZHANG SAN"

    assert extract_card_number(text) == "6222020202020001001"


def test_does_not_treat_id_number_as_card_number() -> None:
    text = "公民身份号码 110101198001010011\n签发机关 TEST"

    assert extract_card_number(text) is None


def test_extracts_valid_date() -> None:
    assert extract_valid_date("VALID THRU 12/30") == "12/30"


def test_extracts_valid_date_with_alternate_separators() -> None:
    assert extract_valid_date("VALID THRU 12-2030") == "12/30"
    assert extract_valid_date("EXP 1230") == "12/30"


def test_does_not_extract_valid_date_from_card_number() -> None:
    assert extract_valid_date("6222 0202 0202 0001") is None


def test_extracts_cardholder_name() -> None:
    text = "TEST BANK\nCARD HOLDER\nZHANG SAN\nVALID THRU 12/30"

    assert extract_cardholder_name(text) == "ZHANG SAN"


def test_extracts_cardholder_name_from_label_line() -> None:
    text = "TEST BANK\nCARDHOLDER NAME: LI SI\nVALID THRU 12/30"

    assert extract_cardholder_name(text) == "LI SI"


def test_invalid_card_number_returns_none() -> None:
    assert normalize_card_number("1234 5678 9012") is None
    assert extract_card_number("CARD 6222 0202 0202") is None


def test_missing_fields_do_not_crash() -> None:
    fields = parse_bank_card_fields("UNRELATED OCR TEXT")

    assert fields == {
        "card_number": None,
        "valid_date": None,
        "name": None,
    }
