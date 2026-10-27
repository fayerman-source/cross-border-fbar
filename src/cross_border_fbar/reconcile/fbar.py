"""FBAR household totals. Separate from Form 8938, which has different rules and thresholds.

The $10,000 test aggregates every reportable account, including signature
authority without financial interest (31 CFR 1010.350; IRS FBAR practice unit).
This module reports whether the total is above it, for review; it never says
whether anyone must file.
"""

from __future__ import annotations

from dataclasses import dataclass

from .accounts import AccountResult

FBAR_AGGREGATE_USD = 10_000


@dataclass
class FbarTotals:
    aggregate_max_usd: int
    accounts_counted: int
    accounts_without_value: list[str]
    above_aggregate_threshold: bool
    crossing_depends_on_flags: bool


def fbar_totals(accounts: list[AccountResult]) -> FbarTotals:
    valued = [a for a in accounts if a.max_value_usd is not None]
    total = sum(a.max_value_usd for a in valued)
    possible = total + sum(
        f.extra_usd or 0 for a in accounts for f in a.flags if f.code == "possible_intraday_peak"
    )
    return FbarTotals(
        aggregate_max_usd=total,
        accounts_counted=len(valued),
        accounts_without_value=[a.account_number for a in accounts if a.max_value_usd is None],
        above_aggregate_threshold=total > FBAR_AGGREGATE_USD,
        crossing_depends_on_flags=(total <= FBAR_AGGREGATE_USD) != (possible <= FBAR_AGGREGATE_USD),
    )
