"""Reconcile observations into FBAR account values. Deterministic; the model only read the pages.

Rules (FinCEN "Reporting Maximum Account Value"; IRS IRM 4.26.16):
- An account's maximum is the largest balance that appears on any of its
  statements: opening, closing or a running balance. When a statement prints no
  running balances, they are computed from its lines and marked as computed.
- Convert at the Treasury rate for Dec 31 of the tax year and round up to the
  next whole dollar; a negative maximum reports as 0.
- What can't be established from the documents is a review flag, never a guess:
  missing months, statements that don't chain, lines whose arithmetic doesn't
  add up, a possible peak between printed lines, unknown ownership.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, Decimal

from ..extract.parse import normalize_name
from ..rates import Rate
from .observe import Observation, Source, most_common


@dataclass(frozen=True)
class Intake:
    """What the client or CPA states about an account; statements rarely say it."""

    ownership: str  # sole | joint | signature_authority | unknown
    financial_interest: bool | None = None


@dataclass
class Flag:
    code: str
    severity: str  # info | review
    message: str
    sources: list[Source] = field(default_factory=list)
    month: str | None = None  # YYYY-MM, for period flags
    extra_usd: int | None = None


@dataclass
class Balance:
    amount: Decimal
    source: Source
    kind: str  # printed | computed
    day: date | None


@dataclass
class AccountResult:
    account_key: str
    account_number: str
    institutions: list[str]
    currency: str | None
    holders: list[str]
    ownership: str
    financial_interest: bool | None
    uploads: list[str]
    duplicate_uploads: list[str]
    max_value: Balance | None
    max_value_usd: int | None
    year_end_balance: Decimal | None
    flags: list[Flag]


def usd_ceiling(amount: Decimal, rate: Rate) -> int:
    if amount <= 0:
        return 0
    return int((amount / rate.units_per_usd).to_integral_value(rounding=ROUND_CEILING))


def _months(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _adjacent(prev: Observation, cur: Observation) -> bool:
    if not (prev.period_end and cur.period_start):
        return True
    return (cur.period_start - prev.period_end).days <= 1


def _distinct_institutions(names: list[str]) -> list[str]:
    """Distinct banks in order; a bilingual header matches its single-language form."""
    out: list[str] = []
    for name in names:
        norm = normalize_name(name)
        if any(normalize_name(seen) in norm or norm in normalize_name(seen) for seen in out):
            continue
        out.append(name)
    return out


def _dedupe(obs: list[Observation]) -> tuple[list[Observation], list[str]]:
    """The same statement uploaded twice: same period and the same opening and closing balances."""
    seen, kept, dupes = set(), [], []
    for o in obs:
        key = (o.period_start, o.period_end, o.opening, o.closing)
        if key in seen:
            dupes.append(o.upload)
        else:
            seen.add(key)
            kept.append(o)
    return kept, dupes


def _balances(o: Observation, flags: list[Flag]) -> list[Balance]:
    """Every balance a reader can get from one statement, checking printed against computed."""
    out = []
    if o.opening is not None:
        out.append(Balance(o.opening, o.sources["opening_balance"], "printed", o.period_start))
    running = o.opening
    for line in o.lines:
        if running is not None and line.amount is not None:
            running += line.amount
        if line.printed_balance is not None:
            if running is not None and line.amount is not None and running != line.printed_balance:
                flags.append(
                    Flag(
                        "arithmetic_mismatch",
                        "review",
                        f"printed balance {line.printed_balance} != computed {running}",
                        [line.source],
                    )
                )
            running = line.printed_balance
            out.append(Balance(line.printed_balance, line.source, "printed", line.day))
        elif running is not None:
            out.append(Balance(running, line.source, "computed", line.day))
    if o.closing is not None:
        if running is not None and o.lines and running != o.closing:
            flags.append(
                Flag(
                    "arithmetic_mismatch",
                    "review",
                    f"closing balance {o.closing} != computed {running}",
                    [o.sources["closing_balance"]],
                )
            )
        out.append(Balance(o.closing, o.sources["closing_balance"], "printed", o.period_end))
    return out


def _intraday(o: Observation) -> list[tuple[Decimal, list[Source], date]]:
    """Per day: the balance if credits had posted first, when higher than any balance printed that day."""
    by_day: dict[date, list] = defaultdict(list)
    for line in o.lines:
        if line.day and line.amount is not None:
            by_day[line.day].append(line)
    out, running = [], o.opening
    for day in sorted(by_day):
        lines = by_day[day]
        if running is None:
            break
        upper = running + sum(ln.amount for ln in lines if ln.amount > 0)
        end = running + sum(ln.amount for ln in lines)
        shown = max([running + sum(ln.amount for ln in lines[: i + 1]) for i in range(len(lines))])
        if upper > shown:
            out.append((upper, [ln.source for ln in lines], day))
        running = end
    return out


def reconcile_account(
    obs: list[Observation], intake: Intake | None, rate: Rate | None, tax_year: int, threshold_usd: int
) -> AccountResult:
    obs = sorted(obs, key=lambda o: (o.period_start or date.min, o.upload))
    obs, dupes = _dedupe(obs)
    flags: list[Flag] = []

    covered = {
        m for o in obs if o.period_start and o.period_end for m in _months(o.period_start, o.period_end)
    }
    for m in _months(date(tax_year, 1, 1), date(tax_year, 12, 31)):
        if m not in covered:
            flags.append(Flag("missing_statement", "review", f"no statement covers {m}", month=m))
    for prev, cur in zip(obs, obs[1:], strict=False):
        if not _adjacent(prev, cur):
            continue  # a gap is already a missing_statement flag
        if prev.closing is not None and cur.opening is not None and prev.closing != cur.opening:
            flags.append(
                Flag(
                    "continuity_break",
                    "review",
                    f"closing {prev.closing} does not carry into opening {cur.opening}",
                    [prev.sources["closing_balance"], cur.sources["opening_balance"]],
                )
            )

    balances = [b for o in obs for b in _balances(o, flags)]
    best = max(balances, key=lambda b: b.amount) if balances else None
    best_usd = usd_ceiling(best.amount, rate) if best and rate else None
    if best and best.source.ref.startswith("row:"):
        flags.append(
            Flag(
                "max_not_at_month_end",
                "info",
                "the maximum is a mid-period line, not a period-end balance",
                [best.source],
            )
        )
    if best and best.kind == "computed":
        flags.append(
            Flag(
                "max_from_computed_balance",
                "info",
                "the statement prints no running balance; computed",
                [best.source],
            )
        )

    if best and rate:
        for upper, sources, day in (c for o in obs for c in _intraday(o)):
            if upper > best.amount:
                extra = usd_ceiling(upper, rate) - best_usd
                if extra >= threshold_usd:
                    flags.append(
                        Flag(
                            "possible_intraday_peak",
                            "review",
                            f"crediting first on {day} would show {upper}, about ${extra:,} above the "
                            "reported maximum",
                            sources,
                            extra_usd=extra,
                        )
                    )

    institutions = _distinct_institutions([o.institution for o in obs if o.institution])
    if len(institutions) > 1:
        flags.append(Flag("institution_changed", "info", "statements come from " + " -> ".join(institutions)))
    currencies = {o.currency for o in obs if o.currency}
    if len(currencies) > 1:
        flags.append(Flag("currency_conflict", "review", f"statements show {sorted(currencies)}"))
    if rate is None:
        flags.append(Flag("currency_unknown", "review", "no Treasury rate for this account's currency"))

    ownership = intake.ownership if intake else "unknown"
    if ownership == "signature_authority":
        flags.append(
            Flag("signature_authority_only", "review", "signature authority without financial interest")
        )
    elif ownership == "unknown":
        flags.append(Flag("ownership_unknown", "review", "ownership not stated at intake"))

    dec = [o for o in obs if o.period_end == date(tax_year, 12, 31)]
    return AccountResult(
        account_key=obs[0].account_key if obs else "",
        account_number=most_common(o.account_number for o in obs) or "",
        institutions=institutions,
        currency=most_common(o.currency for o in obs),
        holders=list(dict.fromkeys(o.holder for o in obs if o.holder)),
        ownership=ownership,
        financial_interest=intake.financial_interest if intake else None,
        uploads=[o.upload for o in obs],
        duplicate_uploads=dupes,
        max_value=best,
        max_value_usd=best_usd,
        year_end_balance=dec[-1].closing if dec else None,
        flags=flags,
    )


def reconcile(
    observations: list[Observation],
    intake: dict[str, Intake],
    rates: dict[str, Rate],
    tax_year: int,
    threshold_usd: int = 1000,
) -> list[AccountResult]:
    """Group pages by account number and reconcile each account."""
    groups: dict[str, list[Observation]] = defaultdict(list)
    for o in observations:
        groups[o.account_key or f"unidentified:{o.upload}"].append(o)
    results = []
    for key, obs in sorted(groups.items()):
        currency = most_common(o.currency for o in obs)
        results.append(
            reconcile_account(obs, intake.get(key), rates.get(currency or ""), tax_year, threshold_usd)
        )
    return results
