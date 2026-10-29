"""Form 8938 totals: indicative, separate from FBAR, every input stated rather than assumed."""

import pytest

from cross_border_fbar.reconcile.accounts import AccountResult, Intake
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
        "PARENTS": "no financial interest (signature authority only)",
        "ASK": "financial interest not stated",
    }
    assert (t.year_end.low_usd, t.any_time.high_usd) == (90_000, 100_000)


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
    assert (t.year_end.threshold_usd, t.any_time.threshold_usd) == want


@pytest.mark.parametrize(
    "household", [Household(), Household("mfs"), Household(lives_abroad=True), Household("x", True)]
)
def test_no_threshold_until_status_and_residence_are_stated(household):
    t = form8938_totals([acct("A", 900_000, 900_000)], household, {})
    assert (t.year_end.result, t.any_time.result) == ("needs_intake", "needs_intake")
    assert t.any_time.high_usd == 900_000  # the totals are still shown


def test_threshold_is_more_than_not_at_least():
    t = form8938_totals([acct("A", 300_000, 200_000)], ABROAD_MFS, {})
    assert (t.year_end.result, t.any_time.result) == ("not_above", "not_above")


def test_maximums_on_different_days_bound_the_any_time_total_without_deciding_it():
    # Each maximum alone is under $300,000 and so is the year-end total; their sum is over.
    t = form8938_totals([acct("A", 199_100, 150_000), acct("B", 127_355, 20_000)], ABROAD_MFS, {})
    assert (t.any_time.low_usd, t.any_time.high_usd) == (199_100, 326_455)
    assert t.any_time.result == "between"


def test_a_lower_bound_over_the_threshold_is_above_even_with_values_missing():
    t = form8938_totals([acct("A", 310_000, 250_000), acct("MMDA", None, None)], ABROAD_MFS, {})
    assert t.accounts_without_value == ["MMDA"]
    assert (t.year_end.result, t.any_time.result) == ("above", "above")


def test_missing_values_leave_an_under_threshold_total_incomplete():
    t = form8938_totals([acct("A", 100_000, 90_000), acct("MMDA", None, None)], ABROAD_MFS, {})
    assert (t.year_end.result, t.any_time.result) == ("incomplete", "incomplete")


def test_separate_filers_who_are_both_specified_individuals_count_half_of_a_joint_account():
    household = Household("mfs", lives_abroad=True, spouse_specified_individual=True)
    intake = {"J": Intake("joint", True, joint_with_spouse=True)}
    t = form8938_totals([acct("J", 127_355, 100_001, ownership="joint")], household, intake)
    assert t.halved == ["J"]
    assert (t.year_end.low_usd, t.any_time.high_usd) == (50_001, 63_678)  # halves round up


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
    totals = form8938_totals([account], Household("single", False), {})
    assert not totals.any_time.complete
    assert totals.any_time.result == "above"
    assert totals.any_time.high_usd is None
