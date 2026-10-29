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
    unresolved_accounts: dict[str, str] = field(default_factory=dict)


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


@dataclass(frozen=True)
class _AccountValue:
    number: str
    year_end: int
    maximum: int
    year_end_complete: bool
    annual_complete: bool
    half: bool
    note: str | None


def _select_accounts(
    accounts: list[AccountResult],
) -> tuple[list[AccountResult], dict[str, str], dict[str, str]]:
    counted, excluded, unresolved = [], {}, {}
    for account in accounts:
        if not account.identified:
            label = account.account_number or ", ".join(account.uploads) or account.account_key
            unresolved[label] = "account could not be identified or page could not be read"
            continue
        if account.financial_interest is False:
            excluded[account.account_number] = "no financial interest"
        elif account.financial_interest is None:
            excluded[account.account_number] = "financial interest not stated"
            unresolved[account.account_number] = "financial interest not stated"
        else:
            counted.append(account)
    return counted, excluded, unresolved


def _joint_share(
    account: AccountResult, household: Household, intake: dict[str, Intake]
) -> tuple[bool, str | None]:
    if household.filing_status != "mfs" or account.ownership != "joint":
        return False, None
    joint_with_spouse = intake.get(account.account_key, Intake("unknown")).joint_with_spouse
    if joint_with_spouse and household.spouse_specified_individual:
        return True, None
    if joint_with_spouse is None or household.spouse_specified_individual is None:
        return False, (
            f"{account.account_number}: counted at full value; half if the joint owner is a spouse who "
            "is also a specified individual"
        )
    return False, None


def _account_value(account: AccountResult, household: Household, intake: dict[str, Intake]) -> _AccountValue:
    half, note = _joint_share(account, household, intake)
    annual_complete = account.max_value_usd is not None and account.annual_max_complete
    return _AccountValue(
        account.account_number,
        _part(account.year_end_usd or 0, half),
        _part(account.max_value_usd or 0, half),
        account.year_end_usd is not None,
        annual_complete,
        half,
        note,
    )


def _thresholds(household: Household) -> tuple[int | None, int | None]:
    if household.filing_status not in FILING_STATUSES or household.lives_abroad is None:
        return None, None
    return THRESHOLDS[(household.lives_abroad, household.filing_status == "mfj")]


def _test(threshold: int | None, low: int, high: int, complete: bool) -> Test:
    return Test(threshold, low, high if complete else None, complete, _result(threshold, low, high, complete))


def form8938_totals(
    accounts: list[AccountResult], household: Household, intake: dict[str, Intake]
) -> Form8938Totals:
    counted, excluded, unresolved = _select_accounts(accounts)
    values = [_account_value(account, household, intake) for account in counted]
    year_end_sum = sum(value.year_end for value in values)
    max_sum = sum(value.maximum for value in values)
    largest_max = max((value.maximum for value in values), default=0)
    year_end_t, any_time_t = _thresholds(household)
    notes = [value.note for value in values if value.note]
    if household.filing_status == "mfj":
        notes.append("a joint return counts both spouses' assets; only the accounts read here are included")
    notes.append("accounts only; other specified foreign financial assets are not included")
    return Form8938Totals(
        year_end=_test(
            year_end_t,
            year_end_sum,
            year_end_sum,
            not unresolved and all(value.year_end_complete for value in values),
        ),
        any_time=_test(
            any_time_t,
            max(year_end_sum, largest_max),
            max_sum,
            not unresolved and all(value.annual_complete for value in values),
        ),
        accounts_counted=[account.account_number for account in counted],
        accounts_without_value=[
            value.number for value in values if not (value.year_end_complete and value.annual_complete)
        ],
        excluded=excluded,
        halved=[value.number for value in values if value.half],
        notes=notes,
        unresolved_accounts=unresolved,
    )
