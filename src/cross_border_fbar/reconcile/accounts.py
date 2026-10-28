"""Reconcile observations into FBAR account values. Deterministic; the model only read the pages.

Rules (FinCEN "Reporting Maximum Account Value"; IRS IRM 4.26.16):
- An account's maximum is the largest balance that appears on any of its
  statements during the tax year: opening, closing or a running balance. When
  a statement prints no running balances, they are computed from its lines and
  marked as computed. Balances dated outside the tax year don't count.
- Convert at the Treasury rate for Dec 31 of the tax year and round up to the
  next whole dollar; a negative maximum reports as 0. USD accounts need no rate.
- What can't be established from the documents is a review flag, never a guess:
  uncovered days, statements that don't chain, lines whose arithmetic doesn't
  add up, unreadable key fields, a possible peak between printed lines,
  unidentified pages, unknown ownership or currency.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal

from ..extract.parse import account_key, normalize_name
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
    month: str | None = None  # YYYY-MM, for missing_statement
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
    identified: bool
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


def _in_year(day: date | None, tax_year: int) -> bool:
    return day is not None and day.year == tax_year


def _adjacent(prev: Observation, cur: Observation) -> bool:
    if not (prev.period_end and cur.period_start):
        return False
    return (cur.period_start - prev.period_end).days <= 1


def _script(ch: str) -> str:
    """A letter's Unicode script from its character name: LATIN (incl. Vietnamese ệ), THAI, HANGUL..."""
    return unicodedata.name(ch, "UNKNOWN").split()[0]


def _script_runs(name: str) -> list[str]:
    """Split a bank-name reading into runs of one script: 'ธนาคารสยามโลตัส Siam Lotus Bank' -> two names."""
    runs, current, script = [], [], None
    for ch in name:
        if ch.isalpha():
            s = _script(ch)
            if script is not None and s != script and current:
                runs.append("".join(current))
                current = []
            script = s
        current.append(ch)
    runs.append("".join(current))
    return [normalize_name(r) for r in runs if normalize_name(r)]


def _distinct_institutions(names: list[str]) -> list[str]:
    """Distinct banks in order. Two names are one bank when one contains the other, or when some page
    prints them together (a bilingual header): the only evidence of an alias. A different script alone
    is not evidence, so a real change of bank is still reported."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            x = parent[x]
        return x

    for name in names:  # a page printing several runs links them
        runs = _script_runs(name)
        for r in runs[1:]:
            parent[find(r)] = find(runs[0])

    out: list[str] = []
    for name in names:
        norm, roots = normalize_name(name), {find(r) for r in _script_runs(name)}
        seen_same = any(
            normalize_name(s) in norm
            or norm in normalize_name(s)
            or roots & {find(r) for r in _script_runs(s)}
            for s in out
        )
        if not seen_same:
            out.append(name)
    return out


def _signature(o: Observation):
    """Identity of a statement's content; None when key fields are missing, so nothing is merged blindly."""
    if None in (o.period_start, o.period_end, o.opening, o.closing):
        return None
    lines = tuple((ln.day, ln.amount, ln.printed_balance) for ln in o.lines)
    return (o.period_start, o.period_end, o.opening, o.closing, lines)


def _dedupe(obs: list[Observation], flags: list[Flag]) -> tuple[list[Observation], list[str]]:
    """Drop exact re-uploads of the same statement; anything less than identical is kept."""
    first: dict = {}
    kept, dupes = [], []
    for o in obs:
        sig = _signature(o)
        if sig is not None and sig in first:
            dupes.append(o.upload)
            flags.append(
                Flag(
                    "duplicate_upload",
                    "info",
                    f"same statement as {first[sig]}",
                    [Source(o.upload, "page", None)],
                )
            )
        else:
            if sig is not None:
                first[sig] = o.upload
            kept.append(o)
    return kept, dupes


def _uncovered(obs: list[Observation], tax_year: int) -> list[tuple[date, date]]:
    """Ranges of days in the tax year that no statement period covers."""
    covered = set()
    for o in obs:
        if o.period_start and o.period_end:
            d = max(o.period_start, date(tax_year, 1, 1))
            while d <= min(o.period_end, date(tax_year, 12, 31)):
                covered.add(d)
                d += timedelta(days=1)
    ranges, start, d = [], None, date(tax_year, 1, 1)
    while d <= date(tax_year, 12, 31) + timedelta(days=1):
        missing = d.year == tax_year and d not in covered
        if missing and start is None:
            start = d
        elif not missing and start is not None:
            ranges.append((start, d - timedelta(days=1)))
            start = None
        d += timedelta(days=1)
    return ranges


def _coverage_flags(obs: list[Observation], tax_year: int, flags: list[Flag]) -> None:
    for start, end in _uncovered(obs, tax_year):
        d = start
        while d <= end:  # whole calendar months become missing_statement; the rest coverage_gap
            month_end = date(d.year + d.month // 12, d.month % 12 + 1, 1) - timedelta(days=1)
            if d.day == 1 and month_end <= end:
                m = f"{d.year}-{d.month:02d}"
                flags.append(Flag("missing_statement", "review", f"no statement covers {m}", month=m))
                d = month_end + timedelta(days=1)
            else:
                stop = min(end, month_end)
                flags.append(Flag("coverage_gap", "review", f"no statement covers {d} to {stop}"))
                d = stop + timedelta(days=1)


def _seed(o: Observation, prev: Observation | None) -> tuple[Decimal | None, str]:
    """Balance before the first line: printed opening, else the previous adjacent closing, else derived."""
    if o.opening is not None:
        return o.opening, "printed"
    if prev is not None and prev.closing is not None and _adjacent(prev, o):
        return prev.closing, "computed"
    for line in o.lines:
        if line.printed_balance is not None and line.amount is not None:
            return line.printed_balance - line.amount, "computed"
        if line.amount is None:
            break
    return None, "computed"


def _balances(o: Observation, prev: Observation | None, flags: list[Flag]) -> list[Balance]:
    """Every balance a reader can get from one statement, checking printed against computed."""
    out = []
    running, seed_kind = _seed(o, prev)
    if o.opening is not None:
        out.append(Balance(o.opening, o.sources["opening_balance"], "printed", o.period_start))
    for line in o.lines:
        if running is not None:
            running = running + line.amount if line.amount is not None else None
        if line.printed_balance is not None:
            if running is not None and running != line.printed_balance:
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


def _intraday(
    o: Observation, prev: Observation | None, tax_year: int
) -> list[tuple[Decimal, list[Source], date]]:
    """Same-day runs, in printed order: the balance if credits had posted first, when above all shown."""
    out = []
    running, _ = _seed(o, prev)
    i = 0
    while i < len(o.lines):
        j = i
        while j + 1 < len(o.lines) and o.lines[j + 1].day == o.lines[i].day:
            j += 1
        run = o.lines[i : j + 1]
        i = j + 1
        if running is None or any(ln.amount is None for ln in run):
            running = run[-1].printed_balance  # resync where the statement prints one
            continue
        shown, bal = [], running
        for ln in run:
            bal += ln.amount
            shown.append(bal)
        upper = running + sum(ln.amount for ln in run if ln.amount > 0)
        if _in_year(run[0].day, tax_year) and upper > max(shown):
            out.append((upper, [ln.source for ln in run], run[0].day))
        running = run[-1].printed_balance if run[-1].printed_balance is not None else bal
    return out


def _field_flags(obs: list[Observation], flags: list[Flag]) -> None:
    for o in obs:
        for name in o.problems:
            flags.append(
                Flag(
                    "field_unreadable",
                    "review",
                    f"{name} could not be read or parsed",
                    [o.sources.get(name, Source(o.upload, name, None))],
                )
            )


def _max_value(balances: list[Balance]) -> Balance | None:
    # On ties prefer printed over computed, and a period-end balance over a mid-period line.
    if not balances:
        return None
    return max(balances, key=lambda b: (b.amount, b.kind == "printed", not b.source.ref.startswith("row:")))


def reconcile_account(
    obs: list[Observation],
    intake: Intake | None,
    rate: Rate | None,
    tax_year: int,
    threshold_usd: int,
    identified: bool = True,
) -> AccountResult:
    flags: list[Flag] = []
    obs = sorted(obs, key=lambda o: (o.period_start or date.min, o.upload))
    out_of_year = [
        o
        for o in obs
        if o.period_end and o.period_end.year < tax_year or o.period_start and o.period_start.year > tax_year
    ]
    for o in out_of_year:
        flags.append(
            Flag(
                "out_of_year_statement",
                "info",
                f"{o.period_start} to {o.period_end} is outside {tax_year}",
                [Source(o.upload, "page", None)],
            )
        )
    obs = [o for o in obs if o not in out_of_year]
    obs, dupes = _dedupe(obs, flags)
    _field_flags(obs, flags)
    _coverage_flags(obs, tax_year, flags)

    prevs = [None, *obs[:-1]]
    for prev, cur in zip(prevs[1:], obs[1:], strict=True):
        if (
            _adjacent(prev, cur)
            and prev.closing is not None
            and cur.opening is not None
            and prev.closing != cur.opening
        ):
            flags.append(
                Flag(
                    "continuity_break",
                    "review",
                    f"closing {prev.closing} does not carry into opening {cur.opening}",
                    [prev.sources["closing_balance"], cur.sources["opening_balance"]],
                )
            )

    balances = [
        b
        for o, p in zip(obs, prevs, strict=True)
        for b in _balances(o, p, flags)
        if _in_year(b.day, tax_year)
    ]
    best = _max_value(balances)
    if best is None and identified and any(o.lines for o in obs):
        flags.append(
            Flag(
                "no_balance_anchor",
                "review",
                "transactions only: no balance appears on any document, so "
                "the maximum can't be determined; request statements showing balances",
            )
        )
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
                "computed from the lines; not printed on the statement",
                [best.source],
            )
        )

    if best and rate:
        for upper, sources, day in (
            c for o, p in zip(obs, prevs, strict=True) for c in _intraday(o, p, tax_year)
        ):
            extra = usd_ceiling(upper, rate) - best_usd
            if upper > best.amount and extra >= threshold_usd:
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
    if not identified:
        masked = obs[0].account_key.startswith("masked:") if obs else False
        code, why = (
            ("account_number_masked", "account number partly masked; can't be matched to an account")
            if masked
            else ("account_unidentified", "account number unreadable")
        )
        flags.append(
            Flag(
                code,
                "review",
                f"{why}; not counted in totals",
                [Source(o.upload, "account_number", None) for o in obs],
            )
        )
    elif ownership == "signature_authority":
        flags.append(
            Flag("signature_authority_only", "review", "signature authority without financial interest")
        )
    elif ownership == "unknown":
        flags.append(Flag("ownership_unknown", "review", "ownership not stated at intake"))

    dec = [o for o in obs if o.period_end == date(tax_year, 12, 31)]
    return AccountResult(
        account_key=obs[0].account_key if obs else "",
        account_number=most_common(o.account_number for o in obs) or "",
        identified=identified,
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


def _rate_for(currency: str | None, rates: dict[str, Rate], tax_year: int) -> Rate | None:
    if currency == "USD":
        return Rate("USD", Decimal(1), f"{tax_year}-12-31", "no conversion: account held in USD")
    return rates.get(currency or "")


def split_by_row_account(o: Observation) -> list[Observation]:
    """Split a page listing several accounts into one observation per account.

    Rows that belong to the page's own (header) account keep the page's opening and closing
    balances; a statement that repeats its account number on every row stays whole.
    """
    if not any(ln.account_text for ln in o.lines):
        return [o]
    groups: dict[str, list] = defaultdict(list)
    for ln in o.lines:
        groups[account_key(ln.account_text) if ln.account_text else o.account_key].append(ln)
    if set(groups) == {o.account_key}:
        return [o]
    keep = [p for p in o.problems if p not in ("account_number", "opening_balance", "closing_balance")]
    parts = []
    for key, lines in groups.items():
        if key and key == o.account_key:
            parts.append(replace(o, lines=lines))
        else:
            text = next((ln.account_text for ln in lines if ln.account_text), "")
            parts.append(
                replace(
                    o,
                    account_key=key,
                    account_number=text,
                    lines=lines,
                    opening=None,
                    closing=None,
                    problems=keep,
                )
            )
    return parts


def reconcile(
    observations: list[Observation],
    intake: dict[str, Intake],
    rates: dict[str, Rate],
    tax_year: int,
    threshold_usd: int = 1000,
) -> list[AccountResult]:
    """Group by account number and reconcile each account. Pages or rows whose account can't be
    identified (unreadable or partly masked) stay apart and are not counted in totals."""
    groups: dict[str, list[Observation]] = defaultdict(list)
    unidentified = []
    for o in (part for page in observations for part in split_by_row_account(page)):
        (groups[o.account_key] if o.account_key else unidentified).append(o)
    results = []
    for key, obs in sorted(groups.items()):
        rate = _rate_for(most_common(o.currency for o in obs), rates, tax_year)
        identified = not key.startswith("masked:")
        results.append(
            reconcile_account(
                obs,
                intake.get(key) if identified else None,
                rate,
                tax_year,
                threshold_usd,
                identified=identified,
            )
        )
    for o in unidentified:
        rate = _rate_for(o.currency, rates, tax_year)
        results.append(reconcile_account([o], None, rate, tax_year, threshold_usd, identified=False))
    return results
