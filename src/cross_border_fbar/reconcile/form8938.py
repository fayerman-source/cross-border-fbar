"""Form 8938 totals, indicative only. Separate from FBAR: different accounts, values and thresholds.

- Counts only accounts in which the client has a financial interest; signature authority alone is not
  an interest in a specified foreign financial asset (Form 8938 instructions, Interests in Specified
  Foreign Financial Assets).
- Two tests: the total on the last day of the tax year, and the total at any time during it, each
  "more than" a threshold set by filing status and residence (26 CFR 1.6038D-2(a)).
- Values convert at the Treasury rate for the last day of the tax year, the same rate FBAR uses here.
- A joint owner counts the whole value, except that spouses who are both specified individuals and file
  separately each count half (instructions, Joint Interest Valuation).
- The any-time total is not the sum of each account's maximum, since maximums can fall on different
  days. That sum is an upper bound; the year-end total and the largest single maximum are lower bounds.
  When the threshold sits between them, the result says so instead of picking a side.

Only accounts read from statements are included; other specified foreign financial assets are not. The
CPA determines filing status, residence and every input here. Nothing here says whether anyone must file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .accounts import AccountResult, Intake

# (lives_abroad, joint_return) -> (last day of the year, any time during the year), each "more than".
THRESHOLDS = {
    (False, False): (50_000, 75_000),
    (False, True): (100_000, 150_000),
    (True, False): (200_000, 300_000),
    (True, True): (400_000, 600_000),
}
FILING_STATUSES = {"single", "mfj", "mfs", "hoh", "qss"}


@dataclass
class Household:
    """What the client or CPA states; statements don't say it."""

    filing_status: str | None = None  # single | mfj | mfs | hoh | qss
    lives_abroad: bool | None = None  # foreign tax home and a presence-abroad test met; the CPA decides
    spouse_specified_individual: bool | None = None  # only matters when filing separately


@dataclass
class Test:
    threshold_usd: int | None  # None until filing status and residence are stated
    low_usd: int  # the total is at least this
    high_usd: int | None  # upper bound only when complete
    complete: bool  # all evidence needed for this particular test is available
    result: str  # above | not_above | between | incomplete | needs_intake


@dataclass
class Form8938Totals:
    year_end: Test
    any_time: Test
    accounts_counted: list[str]
    accounts_without_value: list[str]
    excluded: dict[str, str]  # account number -> why it isn't counted
    halved: list[str]  # joint with a spouse filing separately, both specified individuals
    notes: list[str] = field(default_factory=list)


def _part(usd: int, half: bool) -> int:
    return -(-usd // 2) if half else usd  # a half share rounds up, like every value here


def _result(threshold: int | None, low: int, high: int | None, complete: bool) -> str:
    if threshold is None:
        return "needs_intake"
    if low > threshold:
        return "above"
    if not complete:
        return "incomplete"
    return "not_above" if high <= threshold else "between"


def form8938_totals(
    accounts: list[AccountResult], household: Household, intake: dict[str, Intake]
) -> Form8938Totals:
    excluded, counted, halved, without_value = {}, [], [], []
    for a in accounts:
        if not a.identified:
            continue
        if a.financial_interest is False:
            excluded[a.account_number] = "no financial interest (signature authority only)"
        elif a.financial_interest is None:
            excluded[a.account_number] = "financial interest not stated"
        else:
            counted.append(a)

    filing = household.filing_status if household.filing_status in FILING_STATUSES else None
    notes = []
    year_end_sum = max_sum = largest_max = 0
    year_end_complete = any_time_complete = True
    # These conditions can hide an annual peak even when a December balance is readable.
    annual_uncertainty = {
        "missing_statement",
        "coverage_gap",
        "field_unreadable",
        "no_balance_anchor",
        "no_statements",
        "currency_conflict",
        "arithmetic_mismatch",
        "continuity_break",
        "possible_intraday_peak",
        "no_usable_balance",
    }
    for a in counted:
        half = False
        if filing == "mfs" and a.ownership == "joint":
            joint_with_spouse = intake.get(a.account_key, Intake("unknown")).joint_with_spouse
            if joint_with_spouse and household.spouse_specified_individual:
                half = True
                halved.append(a.account_number)
            elif joint_with_spouse is None or household.spouse_specified_individual is None:
                notes.append(
                    f"{a.account_number}: counted at full value; half if the joint owner is a spouse who "
                    "is also a specified individual"
                )
        annual_missing = a.max_value_usd is None or any(f.code in annual_uncertainty for f in a.flags)
        year_end_missing = a.year_end_usd is None
        any_time_complete = any_time_complete and not annual_missing
        year_end_complete = year_end_complete and not year_end_missing
        if annual_missing or year_end_missing:
            without_value.append(a.account_number)
        if a.year_end_usd is not None:
            year_end_sum += _part(a.year_end_usd, half)
        if a.max_value_usd is not None:
            max_sum += _part(a.max_value_usd, half)
            largest_max = max(largest_max, _part(a.max_value_usd, half))

    if filing is None or household.lives_abroad is None:
        year_end_t = any_time_t = None
    else:
        year_end_t, any_time_t = THRESHOLDS[(household.lives_abroad, filing == "mfj")]
    if filing == "mfj":
        notes.append("a joint return counts both spouses' assets; only the accounts read here are included")
    notes.append("accounts only; other specified foreign financial assets are not included")

    any_time_low = max(year_end_sum, largest_max)
    return Form8938Totals(
        year_end=Test(
            year_end_t,
            year_end_sum,
            year_end_sum if year_end_complete else None,
            year_end_complete,
            _result(year_end_t, year_end_sum, year_end_sum, year_end_complete),
        ),
        any_time=Test(
            any_time_t,
            any_time_low,
            max_sum if any_time_complete else None,
            any_time_complete,
            _result(any_time_t, any_time_low, max_sum, any_time_complete),
        ),
        accounts_counted=[a.account_number for a in counted],
        accounts_without_value=without_value,
        excluded=excluded,
        halved=halved,
        notes=notes,
    )
