"""Boundary cases for intake and indicative comparison serialization."""

from decimal import Decimal

import pytest

from cross_border_fbar.app.form8938 import LABELS, RESULT_TEXT, _test_view
from cross_border_fbar.app.review import OUT_OF_SCOPE
from cross_border_fbar.reconcile.form8938 import Test as Comparison
from cross_border_fbar.reconcile.intake import Household, Intake, IntakeDocument


@pytest.mark.parametrize(
    "field,value",
    [
        ("financial_interest", "false"),
        ("financial_interest", 1),
        ("joint_with_spouse", 0),
        ("ownership", True),
        ("ownership", "owner"),
    ],
)
def test_account_known_fields_reject_non_boolean_and_invalid_values(field, value):
    with pytest.raises(ValueError):
        Intake.from_dict({"account_number": "123", field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("filing_status", True),
        ("filing_status", "married"),
        ("lives_abroad", "false"),
        ("spouse_specified_individual", 1),
    ],
)
def test_household_known_fields_reject_invalid_types_and_values(field, value):
    with pytest.raises(ValueError):
        Household.from_dict({field: value})


def test_intake_preserves_unknowns_metadata_and_rejects_duplicate_normalized_accounts():
    doc = IntakeDocument.from_dict(
        {"source": "synthetic", "other": 4, "accounts": [{"account_number": "12-3", "annotation": "keep"}]}
    )
    assert doc.household.filing_status is None
    assert doc.household.lives_abroad is None
    assert doc.accounts["123"].financial_interest is None
    assert doc.accounts["123"].metadata == {"annotation": "keep"}
    assert doc.metadata == {"other": 4}
    with pytest.raises(ValueError, match="unique"):
        IntakeDocument.from_dict({"accounts": [{"account_number": "12-3"}, {"account_number": "123"}]})


@pytest.mark.parametrize(
    "result,low,high",
    [
        ("above", "50000.0001", None),
        ("not_above", "49999.9999", "50000"),
        ("between", "49999.9999", "50000.0001"),
        ("incomplete", "5", None),
        ("needs_intake", "0", None),
    ],
)
def test_result_messages_keep_exact_decimal_bounds(result, low, high):
    comparison = Comparison(
        None if result == "needs_intake" else 50000,
        Decimal(low),
        Decimal(high) if high else None,
        high is not None,
        result,
    )
    view = _test_view(comparison)
    assert view["low_usd"] == low
    assert view["high_usd"] == high
    assert view["text"] == RESULT_TEXT[result]
    assert "review" in view["text"].lower()
    assert not OUT_OF_SCOPE.search(view["text"])


def test_panel_catalog_has_no_filing_directives():
    for text in [*LABELS.values(), *RESULT_TEXT.values()]:
        assert not OUT_OF_SCOPE.search(text)


def test_display_rounds_outward_without_erasing_a_threshold_crossing():
    above = _test_view(Comparison(50000, Decimal("50000.0001"), Decimal("50000.0002"), True, "above"))
    assert above["display_low_usd"] == "50000.0001"
    assert above["display_high_usd"] == "50000.01"
    below = _test_view(Comparison(50000, Decimal("49999.991"), Decimal("49999.999"), True, "not_above"))
    assert below["display_low_usd"] == "49999.99"
    assert below["display_high_usd"] == "50000.00"
    assert below["high_usd"] == "49999.999"
