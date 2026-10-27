"""The answer key: what a correct reading of the provided statements must conclude.

This is written independently of the product's reconciliation code, so the eval
compares two implementations rather than grading code against itself.

Rules (FinCEN "Reporting Maximum Account Value"; IRS IRM 4.26.16):
- The reportable maximum is the largest balance appearing on any provided
  statement: opening, closing or any running balance. Where a layout prints no
  running balance, balances are computed from the lines and marked "computed".
- Convert at the Treasury Dec 31 rate, round up to the next whole dollar, and
  report a negative maximum as 0.
- A peak between printed lines (same-day credits posted after debits) is never
  reported as the maximum; it is a review flag when it could add at least the
  flag threshold, or would move the household aggregate across $10,000.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_CEILING, Decimal

from ..rates import Rate
from .model import EXPONENT, AccountSpec, Statement, Txn

FBAR_AGGREGATE_USD = 10_000


def major(minor: int, currency: str) -> str:
    return str(Decimal(minor).scaleb(-EXPONENT[currency]))


def to_usd_ceiling(minor: int, currency: str, rate: Rate) -> int:
    if minor <= 0:
        return 0
    usd = Decimal(minor).scaleb(-EXPONENT[currency]) / rate.units_per_usd
    return int(usd.to_integral_value(rounding=ROUND_CEILING))


def _balances_on(stmt: Statement, printed: bool) -> list[tuple[int, str, str]]:
    """(amount, where, kind) for every balance a reader can get from one statement."""
    out = [
        (stmt.opening_balance, "opening_balance", "printed"),
        (stmt.closing_balance, "closing_balance", "printed"),
    ]
    out += [(t.balance_after, t.txn_id, "printed" if printed else "computed") for t in stmt.txns]
    return out


def _intraday_candidates(stmts: list[Statement]) -> list[dict]:
    """Days where crediting first would show a higher balance than any line printed that day."""
    out = []
    for stmt in stmts:
        by_day: dict = defaultdict(list)
        for t in stmt.txns:
            by_day[t.day].append(t)
        balance = stmt.opening_balance
        for day in sorted(by_day):
            txns: list[Txn] = by_day[day]
            upper = balance + sum(t.amount for t in txns if t.amount > 0)
            shown = max(t.balance_after for t in txns)
            if upper > shown:
                out.append(
                    {
                        "statement_id": stmt.statement_id,
                        "date": day.isoformat(),
                        "upper_bound": upper,
                        "highest_printed_that_day": shown,
                        "txn_ids": [t.txn_id for t in txns],
                    }
                )
            balance = txns[-1].balance_after
    return out


def account_truth(
    spec: AccountSpec,
    ledger: list[Txn],
    statements: list[Statement],
    rate: Rate,
    prints_running_balance: dict[str, bool],
    threshold_usd: int,
) -> dict:
    cur = spec.currency
    provided = [s for s in statements if s.month not in spec.missing_months]

    candidates = []
    for s in provided:
        for amount, where, kind in _balances_on(s, prints_running_balance[s.statement_id]):
            candidates.append((amount, s.statement_id, where, kind))
    rep_amount, rep_stmt, rep_where, rep_kind = max(candidates, key=lambda c: c[0])
    month_end = max(provided, key=lambda s: s.closing_balance)
    true_peak = max([spec.opening_balance] + [t.balance_after for t in ledger])

    rep_usd = to_usd_ceiling(rep_amount, cur, rate)
    intraday = []
    for c in _intraday_candidates(provided):
        if c["upper_bound"] > rep_amount:
            extra = to_usd_ceiling(c["upper_bound"], cur, rate) - rep_usd
            intraday.append(
                {
                    **c,
                    "upper_bound": major(c["upper_bound"], cur),
                    "highest_printed_that_day": major(c["highest_printed_that_day"], cur),
                    "possible_extra_usd": extra,
                    "flag": extra >= threshold_usd,
                }
            )

    flags = []
    if rep_where not in ("opening_balance", "closing_balance"):
        flags.append(
            {
                "code": "max_not_at_month_end",
                "severity": "info",
                "statement_id": rep_stmt,
                "source": rep_where,
            }
        )
    flags += [
        {
            "code": "possible_intraday_peak",
            "severity": "review",
            "statement_id": c["statement_id"],
            "date": c["date"],
            "possible_extra_usd": c["possible_extra_usd"],
        }
        for c in intraday
        if c["flag"]
    ]
    if rep_kind == "computed":
        flags.append({"code": "max_from_computed_balance", "severity": "info", "statement_id": rep_stmt})
    for m in spec.missing_months:
        flags.append(
            {
                "code": "missing_statement",
                "severity": "review",
                "statement_id": f"{spec.account_id}_{statements[0].year}-{m:02d}",
            }
        )
    if spec.ownership == "signature_authority":
        flags.append({"code": "signature_authority_only", "severity": "review"})
    if len(spec.banks) > 1:
        flags.append(
            {"code": "institution_changed", "severity": "info", "banks": [b.bank.name for b in spec.banks]}
        )

    dec = statements[-1]
    return {
        "account_id": spec.account_id,
        "currency": cur,
        "account_number": spec.account_number,
        "holder_printed": spec.holder_printed,
        "ownership": spec.ownership,
        "financial_interest": spec.financial_interest,
        "owners": list(spec.owners),
        "institutions": [
            {
                "name": b.bank.name,
                "name_en": b.bank.name_en,
                "first_month": b.first_month,
                "last_month": b.last_month,
            }
            for b in spec.banks
        ],
        "statements_provided": [s.statement_id for s in provided],
        "statements_withheld": [s.statement_id for s in statements if s.month in spec.missing_months],
        "max_value": {
            "amount": major(rep_amount, cur),
            "statement_id": rep_stmt,
            "source": rep_where,
            "kind": rep_kind,
        },
        "max_value_usd": rep_usd,
        "max_month_end": {
            "amount": major(month_end.closing_balance, cur),
            "statement_id": month_end.statement_id,
        },
        "intraday_candidates": intraday,
        "year_end_balance": major(dec.closing_balance, cur) if dec.month not in spec.missing_months else None,
        "hidden_true_peak": {
            "amount": major(true_peak, cur),
            "note": "from the full ledger, including withheld "
            "months; not knowable from the provided statements",
        },
        "expected_flags": flags,
    }


def household_totals(accounts: list[dict], month_end_usd: dict[str, int], threshold_usd: int) -> dict:
    """Aggregate across every reportable account (financial interest or signature authority)."""
    reported = sum(a["max_value_usd"] for a in accounts)
    month_end_only = sum(month_end_usd.values())
    # One maximum per account: its worst case is its largest possible extra, not the sum of them.
    with_intraday = reported + sum(
        max((c["possible_extra_usd"] for c in a["intraday_candidates"]), default=0) for a in accounts
    )
    crosses = (reported <= FBAR_AGGREGATE_USD) != (with_intraday <= FBAR_AGGREGATE_USD) or (
        month_end_only <= FBAR_AGGREGATE_USD
    ) != (reported <= FBAR_AGGREGATE_USD)
    return {
        "aggregate_max_usd": reported,
        "aggregate_month_end_only_usd": month_end_only,
        "aggregate_with_intraday_upper_bounds_usd": with_intraday,
        "above_fbar_aggregate_threshold": reported > FBAR_AGGREGATE_USD,
        "threshold_crossing_depends_on_peaks": crosses,
        "flag_threshold_usd": threshold_usd,
    }
