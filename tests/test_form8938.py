"""Form 8938 totals: indicative, separate from FBAR, every input stated rather than assumed."""

import pytest

from cross_border_fbar.reconcile.accounts import AccountResult, Intake, ValueBounds
from cross_border_fbar.reconcile.form8938 import Household, form8938_totals

ABROAD_MFS = Household("mfs", lives_abroad=True, spouse_specified_individual=False)


def acct(number, max_usd, year_end_usd, ownership="sole", interest=True, identified=True):
    return AccountResult(
        account_key=number,
        account_number=number,
        identified=identified,
        institutions=["Bank"],
        currency="EUR",
        holders=[],
        ownership=ownership,
        financial_interest=interest,
        uploads=[],
        duplicate_uploads=[],
        max_value=None,
        max_value_usd=max_usd,
        year_end_balance=None,
        year_end_usd=year_end_usd,
        flags=[],
        annual_max_complete=True,
        maximum_bounds=ValueBounds(max_usd or 0, max_usd),
        year_end_bounds=ValueBounds(year_end_usd or 0, year_end_usd),
    )


def test_signature_authority_and_unstated_interest_are_not_counted():
    t = form8938_totals(
        [
            acct("A", 100_000, 90_000),
            acct("PARENTS", 500_000, 500_000, ownership="signature_authority", interest=False),
            acct("ASK", 500_000, 500_000, interest=None),
            acct("PAGE", 500_000, 500_000, identified=False),
        ],
        ABROAD_MFS,
        {},
    )
    assert t.accounts_counted == ["A"]
    assert t.excluded == {
        "PARENTS": "no financial interest",
        "ASK": "financial interest not stated",
    }
    assert t.year_end.low_usd == 90000
    assert t.any_time.low_usd == 100000
    assert t.any_time.high_usd is None


@pytest.mark.parametrize(
    "status, abroad, want",
    [
        ("single", False, (50_000, 75_000)),
        ("mfs", False, (50_000, 75_000)),
        ("hoh", False, (50_000, 75_000)),
        ("mfj", False, (100_000, 150_000)),
        ("single", True, (200_000, 300_000)),
        ("mfs", True, (200_000, 300_000)),
        ("mfj", True, (400_000, 600_000)),
    ],
)
def test_thresholds_follow_filing_status_and_residence(status, abroad, want):
    t = form8938_totals([acct("A", 1, 1)], Household(status, abroad), {})
    assert t.year_end.threshold_usd == want[0]
    assert t.any_time.threshold_usd == want[1]


@pytest.mark.parametrize(
    "household", [Household(), Household("mfs"), Household(lives_abroad=True), Household("x", True)]
)
def test_no_threshold_until_status_and_residence_are_stated(household):
    t = form8938_totals([acct("A", 900_000, 900_000)], household, {})
    assert t.year_end.result == "needs_intake"
    assert t.any_time.result == "needs_intake"
    assert t.any_time.high_usd == 900_000  # the totals are still shown


def test_threshold_is_more_than_not_at_least():
    t = form8938_totals([acct("A", 300_000, 200_000)], ABROAD_MFS, {})
    assert t.year_end.result == "not_above"
    assert t.any_time.result == "not_above"


def test_maximums_on_different_days_bound_the_any_time_total_without_deciding_it():
    # Each maximum alone is under $300,000 and so is the year-end total; their sum is over.
    t = form8938_totals([acct("A", 199_100, 150_000), acct("B", 127_355, 20_000)], ABROAD_MFS, {})
    assert t.any_time.low_usd == 199100
    assert t.any_time.high_usd == 326455
    assert t.any_time.result == "between"


def test_a_lower_bound_over_the_threshold_is_above_even_with_values_missing():
    t = form8938_totals([acct("A", 310_000, 250_000), acct("MMDA", None, None)], ABROAD_MFS, {})
    assert t.accounts_without_value == ["MMDA"]
    assert t.year_end.result == "above"
    assert t.any_time.result == "above"


def test_missing_values_leave_an_under_threshold_total_incomplete():
    t = form8938_totals([acct("A", 100_000, 90_000), acct("MMDA", None, None)], ABROAD_MFS, {})
    assert t.year_end.result == "incomplete"
    assert t.any_time.result == "incomplete"


def test_separate_filers_who_are_both_specified_individuals_count_half_of_a_joint_account():
    household = Household("mfs", lives_abroad=True, spouse_specified_individual=True)
    intake = {"J": Intake("joint", True, joint_with_spouse=True)}
    t = form8938_totals([acct("J", 127_355, 100_001, ownership="joint")], household, intake)
    assert t.halved == ["J"]
    assert t.year_end.low_usd == 50001
    assert t.any_time.high_usd == 63678


def test_a_joint_owner_who_is_not_a_specified_individual_leaves_the_full_value():
    intake = {"J": Intake("joint", True, joint_with_spouse=True)}
    t = form8938_totals([acct("J", 127_355, 100_000, ownership="joint")], ABROAD_MFS, intake)
    assert t.halved == []
    assert t.any_time.high_usd == 127_355


def test_an_unstated_joint_owner_is_counted_in_full_with_a_note():
    household = Household("mfs", lives_abroad=True, spouse_specified_individual=None)
    t = form8938_totals([acct("J", 127_355, 100_000, ownership="joint")], household, {})
    assert t.any_time.high_usd == 127_355
    assert any(n.startswith("J: counted at full value") for n in t.notes)


def test_nothing_says_anyone_must_file():
    t = form8938_totals([acct("A", 900_000, 900_000)], ABROAD_MFS, {})
    text = " ".join(t.notes).lower()
    assert not any(word in text for word in ("must", "required", "penalt"))


@pytest.mark.parametrize("code", ["missing_statement", "coverage_gap", "possible_intraday_peak"])
def test_annual_evidence_gaps_do_not_make_the_year_end_test_incomplete(code):
    from cross_border_fbar.reconcile.accounts import Flag

    account = acct("A", 1000, 1000)
    account.flags = [Flag(code, "review", "Synthetic evidence gap")]
    account.annual_max_complete = False
    account.maximum_bounds = ValueBounds(account.max_value_usd or 0, None)
    totals = form8938_totals([account], Household("single", False), {})
    assert totals.year_end.complete
    assert totals.year_end.result == "not_above"
    assert not totals.any_time.complete
    assert totals.any_time.result == "incomplete"
    assert totals.any_time.high_usd is None
    assert totals.accounts_without_value == ["A"]


def test_missing_year_end_does_not_prevent_a_complete_annual_test():
    totals = form8938_totals([acct("A", 1000, None)], Household("single", False), {})
    assert not totals.year_end.complete
    assert totals.year_end.high_usd is None
    assert totals.any_time.complete
    assert totals.any_time.result == "not_above"


def test_observed_lower_bound_can_cross_threshold_despite_missing_annual_coverage():
    from cross_border_fbar.reconcile.accounts import Flag

    account = acct("A", 80000, 1000)
    account.flags = [Flag("missing_statement", "review", "missing January")]
    account.annual_max_complete = False
    account.maximum_bounds = ValueBounds(account.max_value_usd or 0, None)
    totals = form8938_totals([account], Household("single", False), {})
    assert not totals.any_time.complete
    assert totals.any_time.result == "above"
    assert totals.any_time.high_usd is None


def test_sub_tolerance_intraday_uncertainty_prevents_a_below_threshold_annual_result():
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.reconcile.accounts import reconcile

    page = obs(
        "year",
        date(2025, 1, 1),
        date(2025, 12, 31),
        Decimal(74900),
        Decimal(74900),
        [(date(2025, 6, 5), Decimal(-200), Decimal(74700)), (date(2025, 6, 5), Decimal(200), Decimal(74900))],
        currency="USD",
    )
    (account,) = reconcile([page], {"ACC1": Intake("sole", True)}, {}, 2025)
    assert not account.flags
    assert not account.annual_max_complete
    totals = form8938_totals([account], Household("single", False), {})
    assert totals.any_time.result == "between"
    assert totals.any_time.high_usd == 75100
    assert totals.year_end.result == "above"


def test_mixed_currency_accounts_withhold_all_native_and_dollar_values():
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.rates import Rate
    from cross_border_fbar.reconcile.accounts import reconcile

    pages = [
        obs("jan", date(2025, 1, 1), date(2025, 1, 31), Decimal(1000), Decimal(1000), currency="EUR"),
        obs("feb", date(2025, 2, 1), date(2025, 2, 28), Decimal(1000), Decimal(1000), currency="EUR"),
        obs("dec", date(2025, 12, 1), date(2025, 12, 31), Decimal(60000), Decimal(60000), currency="USD"),
    ]
    (account,) = reconcile(
        pages,
        {"ACC1": Intake("sole", True)},
        {"EUR": Rate("EUR", Decimal(2), "2025-12-31", "synthetic rate")},
        2025,
    )
    assert account.max_value is None
    assert account.max_value_usd is None
    assert account.year_end_balance is None
    assert account.year_end_usd is None
    assert not account.annual_max_complete
    totals = form8938_totals([account], Household("single", False), {})
    assert totals.year_end.result == "incomplete"
    assert totals.any_time.result == "incomplete"


@pytest.mark.parametrize(
    "unknown",
    [
        acct("ASK", 900000, 900000, interest=None),
        acct("MASKED", 900000, 900000, identified=False),
    ],
)
def test_unresolved_accounts_make_both_tests_incomplete_without_adding_their_values(unknown):
    totals = form8938_totals([acct("A", 1000, 1000), unknown], Household("single", False), {})
    assert totals.year_end.low_usd == 1000
    assert totals.any_time.low_usd == 1000
    assert totals.year_end.result == "incomplete"
    assert totals.any_time.result == "incomplete"
    assert totals.accounts_counted == ["A"]
    assert unknown.account_number in totals.unresolved_accounts


def test_an_unread_page_is_named_by_upload_and_never_added_to_totals():
    page = acct("PAGE", 900000, 900000, identified=False)
    page.account_number = ""
    page.uploads = ["pages/synthetic-unread.png"]
    totals = form8938_totals([acct("A", 1000, 1000), page], Household("single", False), {})
    assert totals.year_end.result == "incomplete"
    assert list(totals.unresolved_accounts) == ["pages/synthetic-unread.png"]
    assert totals.year_end.low_usd == 1000


def test_known_no_interest_accounts_do_not_make_the_tests_incomplete():
    totals = form8938_totals(
        [acct("A", 1000, 1000), acct("PARENTS", 900000, 900000, interest=False)],
        Household("single", False),
        {},
    )
    assert totals.year_end.result == "not_above"
    assert totals.any_time.result == "not_above"
    assert not totals.unresolved_accounts


def test_known_lower_bound_above_threshold_survives_unresolved_account_identity():
    totals = form8938_totals(
        [acct("A", 80000, 60000), acct("UNREAD", 1, 1, identified=False)], Household("single", False), {}
    )
    assert totals.year_end.result == "above"
    assert totals.any_time.result == "above"
    assert not totals.year_end.complete
    assert not totals.any_time.complete


@pytest.mark.parametrize("kind", ["amount", "date", "anchor"])
def test_missing_transaction_evidence_never_produces_a_complete_annual_bound(kind):
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.reconcile.accounts import reconcile

    amount = None if kind == "amount" else Decimal(1000)
    day = None if kind == "date" else date(2025, 5, 1)
    opening = None if kind == "anchor" else Decimal(1000)
    page = obs(
        "year",
        date(2025, 1, 1),
        date(2025, 12, 31),
        opening,
        Decimal(1000),
        [(day, amount, None)],
        currency="USD",
    )
    (account,) = reconcile([page], {"ACC1": Intake("sole", True)}, {}, 2025)
    assert account.max_value_usd is not None
    assert not account.annual_max_complete
    totals = form8938_totals([account], Household("single", False), {})
    assert totals.any_time.result == "incomplete"
    assert totals.year_end.result == "not_above"


@pytest.mark.parametrize("reverse_names", [False, True])
def test_conflicting_year_end_closings_never_depend_on_upload_names(reverse_names):
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.reconcile.accounts import reconcile

    names = ["z", "a"] if reverse_names else ["a", "z"]
    pages = [
        obs(name, date(2025, 1, 1), date(2025, 12, 31), Decimal(1000), Decimal(closing), currency="USD")
        for name, closing in zip(names, [60000, 1000], strict=True)
    ]
    (account,) = reconcile(pages, {"ACC1": Intake("sole", True)}, {}, 2025)
    assert account.year_end_balance is None
    assert account.year_end_usd is None
    assert not account.annual_max_complete
    totals = form8938_totals([account], Household("single", False), {})
    assert totals.year_end.result == "between"
    assert totals.any_time.result == "not_above"
    assert totals.year_end.low_usd == 1000
    assert totals.year_end.high_usd == 60000


def test_agreeing_year_end_closings_keep_the_value_with_an_absent_continuation_balance():
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.reconcile.accounts import reconcile

    pages = [
        obs("a", date(2025, 1, 1), date(2025, 12, 31), Decimal(1000), Decimal(1000), currency="USD"),
        obs("z", date(2025, 12, 1), date(2025, 12, 31), None, None, currency="USD"),
    ]
    (account,) = reconcile(pages, {"ACC1": Intake("sole", True)}, {}, 2025)
    assert account.year_end_balance == Decimal(1000)
    assert account.year_end_usd == 1000


def test_unknown_dates_with_printed_high_balances_leave_the_annual_upper_bound_unknown():
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.reconcile.accounts import reconcile

    page = obs(
        "year",
        date(2025, 1, 1),
        date(2025, 12, 31),
        Decimal(1000),
        Decimal(1000),
        [(None, Decimal(80000), Decimal(81000)), (None, Decimal(-80000), Decimal(1000))],
        currency="USD",
    )
    (account,) = reconcile([page], {"ACC1": Intake("sole", True)}, {}, 2025)
    assert not account.annual_max_complete
    totals = form8938_totals([account], Household("single", False), {})
    assert totals.any_time.result == "incomplete"
    assert totals.any_time.high_usd is None


@pytest.mark.parametrize(
    "closings,year_result,annual_result",
    [
        ((60000, 1000), "between", "not_above"),
        ((90000, 1000), "between", "between"),
        ((90000, 85000), "above", "above"),
        ((40000, 1000), "not_above", "not_above"),
    ],
)
def test_conflicting_readings_contribute_safe_lower_and_upper_candidates(
    closings, year_result, annual_result
):
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.reconcile.accounts import reconcile

    pages = [
        obs(name, date(2025, 1, 1), date(2025, 12, 31), Decimal(1000), Decimal(value), currency="USD")
        for name, value in zip(["a", "z"], closings, strict=True)
    ]
    (account,) = reconcile(pages, {"ACC1": Intake("sole", True)}, {}, 2025)
    totals = form8938_totals([account], Household("single", False), {})
    assert totals.year_end.low_usd == min(closings)
    assert totals.year_end.high_usd == max(closings)
    assert totals.year_end.result == year_result
    assert totals.any_time.result == annual_result


def test_disputed_computed_paths_do_not_claim_a_finite_annual_upper_bound():
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.reconcile.accounts import reconcile

    pages = [
        obs(
            "a",
            date(2025, 1, 1),
            date(2025, 12, 31),
            Decimal(1000),
            Decimal(1000),
            [(date(2025, 5, 1), Decimal(80000), None), (date(2025, 5, 2), Decimal(-80000), None)],
            currency="USD",
        ),
        obs("z", date(2025, 1, 1), date(2025, 12, 31), Decimal(900), Decimal(1000), currency="USD"),
    ]
    (account,) = reconcile(pages, {"ACC1": Intake("sole", True)}, {}, 2025)
    totals = form8938_totals([account], Household("single", False), {})
    assert totals.any_time.low_usd == 1000
    assert totals.any_time.high_usd is None
    assert totals.any_time.result == "incomplete"


@pytest.mark.parametrize("unknown_month", [1, 12])
def test_currency_is_required_on_the_statement_whose_value_is_converted(unknown_month):
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.rates import Rate
    from cross_border_fbar.reconcile.accounts import reconcile

    pages = [
        obs(
            "jan",
            date(2025, 1, 1),
            date(2025, 1, 31),
            Decimal(1000),
            Decimal(1000),
            currency=None if unknown_month == 1 else "EUR",
            problems=["currency"] if unknown_month == 1 else [],
        ),
        obs(
            "dec",
            date(2025, 12, 1),
            date(2025, 12, 31),
            Decimal(60000),
            Decimal(60000),
            currency=None if unknown_month == 12 else "EUR",
            problems=["currency"] if unknown_month == 12 else [],
        ),
    ]
    (account,) = reconcile(
        pages,
        {"ACC1": Intake("sole", True)},
        {"EUR": Rate("EUR", Decimal(2), "2025-12-31", "synthetic rate")},
        2025,
    )
    totals = form8938_totals([account], Household("single", False), {})
    assert not account.annual_max_complete
    if unknown_month == 12:
        assert account.year_end_usd is None
        assert account.year_end_bounds is None
        assert totals.year_end.result == "incomplete"
        assert account.max_value_usd is None
        assert account.maximum_bounds.low_usd == 500
    else:
        assert account.year_end_usd == 30000
        assert totals.year_end.result == "not_above"


def test_an_absent_currency_also_requires_source_review_instead_of_account_wide_inheritance():
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.reconcile.accounts import reconcile

    page = obs("unknown", date(2025, 1, 1), date(2025, 12, 31), Decimal(60000), Decimal(60000), currency=None)
    (account,) = reconcile([page], {"ACC1": Intake("sole", True)}, {}, 2025)
    assert account.max_value is None
    assert account.year_end_usd is None
    assert any(
        flag.code == "field_unreadable" and flag.sources[0].ref == "currency" for flag in account.flags
    )


def test_missing_exchange_rate_preserves_native_evidence_without_inventing_a_dollar_value():
    from datetime import date
    from decimal import Decimal

    from test_reconcile_edges import obs

    from cross_border_fbar.reconcile.accounts import reconcile

    page = obs("native", date(2025, 1, 1), date(2025, 12, 31), Decimal(40000), Decimal(40000), currency="INR")
    (account,) = reconcile([page], {"ACC1": Intake("sole", True)}, {}, 2025)
    assert account.max_value.amount == Decimal(40000)
    assert account.year_end_balance == Decimal(40000)
    assert account.year_end_usd is None
    assert account.year_end_bounds is None
