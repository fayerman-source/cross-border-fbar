"""Turn a model's page reading into typed observations. Parsing only; no judgement."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..extract.parse import normalize_account_number, parse_amount, parse_date

_CURRENCY_SYMBOLS = {"₩": "KRW", "€": "EUR", "฿": "THB", "$": "USD"}


@dataclass(frozen=True)
class Source:
    """Where a number came from: the uploaded page, the field or row, and its box (0-1000 grid)."""

    upload: str
    ref: str  # "opening_balance" | "closing_balance" | "row:<index>"
    bbox: dict | None


@dataclass
class Line:
    index: int
    day: date | None
    amount: Decimal | None
    printed_balance: Decimal | None
    description: str
    source: Source


@dataclass
class Observation:
    upload: str
    institution: str
    currency: str | None
    account_key: str
    account_number: str
    holder: str
    period_start: date | None
    period_end: date | None
    opening: Decimal | None
    closing: Decimal | None
    lines: list[Line]
    sources: dict[str, Source] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)


def parse_currency(text: str) -> str | None:
    t = (text or "").strip()
    code = re.search(r"\b[A-Z]{3}\b", t.upper())
    if code:
        return code.group(0)
    for sym, iso in _CURRENCY_SYMBOLS.items():
        if sym in t:
            return iso
    return None


def parse_period(text: str) -> tuple[date | None, date | None]:
    parts = re.split(r"\s*(?:~|–|—|\s-\s|\bto\b|\bbis\b)\s*", (text or "").strip())
    if len(parts) != 2:
        return None, None
    start, end = parse_date(parts[0]), parse_date(parts[1])
    return start, end


def _signed(row: dict) -> Decimal | None:
    amount = parse_amount(row["amount_text"])
    if amount is None or row["amount_column"] == "signed":
        return amount
    return -abs(amount) if row["amount_column"] == "debit" else abs(amount)


def observe(reading: dict, upload: str, snapped: dict | None = None) -> Observation:
    """Build an observation from a reading; boxes come from the snapped reading when there is one."""
    boxes_from = snapped or reading

    def value(name: str) -> str:
        f = reading[name]
        return f["text"] if f["status"] == "read" else ""

    start, end = parse_period(value("period"))
    year_hint = (start or end).year if (start or end) else None
    lines = []
    for i, row in enumerate(reading["rows"]):
        src = Source(upload, f"row:{i}", boxes_from["rows"][i]["row_bbox"])
        lines.append(
            Line(
                i,
                parse_date(row["date_text"], year_hint),
                _signed(row),
                parse_amount(row["balance_text"]),
                row["description"],
                src,
            )
        )
    sources = {n: Source(upload, n, reading[n]["bbox"]) for n in ("opening_balance", "closing_balance")}
    return Observation(
        upload=upload,
        institution=value("institution").strip(),
        currency=parse_currency(value("currency")),
        account_key=normalize_account_number(value("account_number")),
        account_number=value("account_number").strip(),
        holder=value("holder").strip(),
        period_start=start,
        period_end=end,
        opening=parse_amount(value("opening_balance")),
        closing=parse_amount(value("closing_balance")),
        lines=lines,
        sources=sources,
        unreadable=[
            n for n in reading if isinstance(reading[n], dict) and reading[n].get("status") == "unreadable"
        ],
    )


def most_common(values) -> str | None:
    c = Counter(v for v in values if v)
    return c.most_common(1)[0][0] if c else None
