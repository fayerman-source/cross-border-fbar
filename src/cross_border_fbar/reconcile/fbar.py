"""FBAR household totals. Separate from Form 8938, which has different rules and thresholds.

The $10,000 test aggregates every reportable account, including signature
authority without financial interest (31 CFR 1010.350; IRS FBAR practice unit).
This module reports whether the total is above it, for review; it never says
whether anyone must file.
"""

from __future__ import annotations

from dataclasses import dataclass

from .accounts import AccountResult
from .flags import CATALOG

FBAR_AGGREGATE_USD = 10_000


@dataclass
class FbarTotals:
    aggregate_max_usd: int
    accounts_counted: int
    accounts_without_value: list[str]
    unidentified_uploads: list[str]
    above_aggregate_threshold: bool | None
    crossing_depends_on_flags: bool
    threshold_review_reasons: list[str]


def fbar_totals(accounts: list[AccountResult]) -> FbarTotals:
    """Aggregate identified accounts. Unidentified pages are listed, not counted."""
    counted = [a for a in accounts if a.identified]
    valued = [a for a in counted if a.max_value_usd is not None]
    total = sum(a.max_value_usd for a in valued)
    # An account has one maximum, so its worst case is its largest possible intraday extra, not the sum.
    possible = total + sum(
        max((f.extra_usd or 0 for f in a.flags if f.code == "possible_intraday_peak"), default=0)
        for a in counted
    )
    reasons = []
    if any(a.max_value_usd is None for a in counted):
        reasons.append("accounts_without_value")
    if any(not a.identified for a in accounts):
        reasons.append("unidentified_accounts_or_pages")
    if any(CATALOG[f.code].may_raise_aggregate for a in accounts for f in a.flags):
        reasons.append("review_items")
    if total > FBAR_AGGREGATE_USD:
        above = True
    elif reasons:
        above = None
    else:
        above = False
    return FbarTotals(
        aggregate_max_usd=total,
        accounts_counted=len(valued),
        accounts_without_value=[a.account_number for a in counted if a.max_value_usd is None],
        unidentified_uploads=[u for a in accounts if not a.identified for u in a.uploads],
        above_aggregate_threshold=above,
        crossing_depends_on_flags=(total <= FBAR_AGGREGATE_USD) != (possible <= FBAR_AGGREGATE_USD),
        threshold_review_reasons=reasons if above is None else [],
    )
