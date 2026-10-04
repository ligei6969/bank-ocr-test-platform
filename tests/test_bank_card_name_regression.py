"""EVT-006: expiry labels, interleaved OCR lines and plausible-name negatives."""

import json
from pathlib import Path

import pytest

from app import field_parser as parser


@pytest.mark.parametrize("expiry", ["VALID THIRU", "VAIID THRU", "GOOD THRV", "EXPIRV"])
@pytest.mark.parametrize("holder", ["CARD HOLDER", "CARD HOLDEN", "CARD H0LDER"])
def test_interleaved_expiry_label_is_not_a_name(expiry, holder):
    text = f"TEST BANK\n5415 9827 3869 1093\n{holder}\n{expiry}\nZHU BIN\n04/36"
    assert parser.extract_cardholder_name(text) == "ZHU BIN"


def test_real_evt006_snapshot_name_is_extracted():
    path = Path(__file__).resolve().parents[1] / "data/annotations/ocr_outputs.json"
    observation = json.loads(path.read_text(encoding="utf-8"))["observations"][
        "data/processed/bank_card/blur/bank_card_0001.png"
    ]
    assert parser.extract_cardholder_name("\n".join(observation["ocr_texts"])) == "ZHU BIN"


@pytest.mark.parametrize("name", ["VALID THOMAS", "GOOD WILLIAMS", "THOMAS EXPIRY", "ZHU BIN", "MARY O'NEIL"])
def test_legitimate_names_are_not_fuzzy_expiry_labels(name):
    assert parser.extract_cardholder_name(f"CARD HOLDER\n{name}\n12/30") == name


def test_damaged_holder_label_prioritizes_its_value_over_other_uppercase_text():
    assert parser.extract_cardholder_name("CUSTOMER SERVICE\nCARD HOLDEN\nLI SI") == "LI SI"


def test_date_digits_must_not_be_erased_to_create_a_name():
    assert parser.extract_cardholder_name("VALID THIRU 12/30\n04/36") is None


def test_card_body_candidate_outranks_unlabeled_header_noise():
    assert parser.extract_cardholder_name("CUSTOMER SERVICE\n5415 9827 3869 1093\nSUN FENG") == "SUN FENG"


def test_fuzzy_expiry_label_without_name_evidence_does_not_invent_a_name():
    assert parser.extract_cardholder_name("VAIID THIRU\n04/36") is None


def test_a_holder_label_does_not_make_an_expiry_label_a_valid_name():
    assert parser.extract_cardholder_name("CARD HOLDER NAME: VALID THIRU\n04/36") is None
