"""An explicit currency correction can settle missing source-unit context."""

from datetime import date
from decimal import Decimal

from test_reconcile_edges import obs

from cross_border_fbar.rates import Rate
from cross_border_fbar.reconcile.accounts import Intake, reconcile


def test_explicit_currency_selection_resolves_unknown_units_without_inheriting_them():
    pages = [
        obs("known", date(2025, 1, 1), date(2025, 11, 30), Decimal(1000), Decimal(1000), currency="EUR"),
        obs(
            "unreadable",
            date(2025, 12, 1),
            date(2025, 12, 31),
            Decimal(1000),
            Decimal(60000),
            currency=None,
            problems=["currency"],
        ),
    ]
    rates = {"EUR": Rate("EUR", Decimal(2), "2025-12-31", "synthetic rate")}
    intake = {"ACC1": Intake("sole", True)}
    (unresolved,) = reconcile(pages, intake, rates, 2025)
    assert unresolved.year_end_usd is None
    (settled,) = reconcile(pages, intake, rates, 2025, currency_selections={"ACC1": "EUR"})
    assert settled.year_end_usd == 30000
    assert settled.maximum_bounds.low_usd == 30000
    assert settled.maximum_bounds.high_usd == 30000
    assert settled.annual_max_complete
