from datetime import date
from decimal import Decimal

import pytest

from cross_border_fbar.extract.parse import (
    normalize_account_number,
    parse_amount,
    parse_date,
    parse_typed_amount,
)


@pytest.mark.parametrize(
    "text, want",
    [
        ("38,074,000", Decimal("38074000")),  # won, thousands commas
        ("₩40,657,677", Decimal("40657677")),
        ("19.318,52 EUR", Decimal("19318.52")),  # German
        ("-310,00", Decimal("-310.00")),
        ("1.234,56 -", Decimal("-1234.56")),  # trailing minus
        ("407,150.25", Decimal("407150.25")),  # Thai
        ("(1,000.00)", Decimal("-1000.00")),
        ("1,377", Decimal("1377")),
    ],
)
def test_parse_amount_locales(text, want):
    assert parse_amount(text) == want


@pytest.mark.parametrize("text", ["", "   ", "-", "n/a"])
def test_parse_amount_none_when_no_number(text):
    assert parse_amount(text) is None


@pytest.mark.parametrize(
    "text, hint, want",
    [
        ("2025.03.10", None, date(2025, 3, 10)),
        ("16/05/2568", None, date(2025, 5, 16)),  # Buddhist era
        ("01.03.2025", None, date(2025, 3, 1)),
        ("01.03.", 2025, date(2025, 3, 1)),  # German booking day without year
        ("31/02/2568", None, None),  # impossible date
        ("01.03.", None, None),  # no year to anchor
    ],
)
def test_parse_date(text, hint, want):
    assert parse_date(text, hint) == want


def test_account_numbers_compare_without_spaces_or_hyphens():
    assert normalize_account_number("DE36 9901 2345") == normalize_account_number("de36-99012345")


@pytest.mark.parametrize(
    "text, value",
    [
        ("287,450,200", "287450200"),
        ("287450200", "287450200"),
        ("1,234.56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("1234.56", "1234.56"),
        ("1234,56", "1234.56"),
        ("1 234,56", "1234.56"),
        ("1,234", "1234"),
        ("1.234", "1234"),
        ("0", "0"),
        (" 500 ", "500"),
        ("-500,000", "-500000"),
        ("500,000-", "-500000"),
        ("(500,000)", "-500000"),
    ],
)
def test_a_typed_amount_is_read_by_the_printed_rules(text, value):
    assert parse_typed_amount(text) == Decimal(value)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "-",
        "()",
        "abc",
        "12abc",
        "1e5",
        "NaN",
        "1.5",
        "1234.5",
        "1,2,3",
        "1,234,56",
        "--5",
        "(5",
        "١٢٣",
        "1" * 25,
    ],
)
def test_a_typed_amount_that_does_not_read_one_clear_way_is_refused(text):
    assert parse_typed_amount(text) is None
