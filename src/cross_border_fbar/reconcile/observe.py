"""Turn a model's page reading into typed observations. Parsing only; no judgement."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..extract.parse import account_key, parse_amount, parse_currency, parse_date


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
    account_text: str = ""  # the row's own account number, on lists covering several accounts


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
    problems: list[str] = field(default_factory=list)  # key fields unreadable, or read but unparseable


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


def _row_date(text: str, start: date | None, end: date | None) -> date | None:
    """A row date; a year-less date in a statement crossing a year end takes the year of its month."""
    d = parse_date(text, start.year if start else end.year if end else None)
    if d and start and end and start.year != end.year and d.year == start.year and d.month < start.month:
        d = d.replace(year=end.year)
    return d


KEY_FIELDS = ("account_number", "period", "currency", "opening_balance", "closing_balance")


def observe(reading: dict, upload: str, snapped: dict | None = None) -> Observation:
    """Build an observation from a reading; boxes come from the snapped reading when there is one."""
    boxes_from = snapped or reading

    def value(name: str) -> str:
        f = reading[name]
        # Text on the page is the evidence: some models return the right text but label it "absent".
        # "unreadable" stays empty, so it can't be mistaken for a reading.
        return f["text"] if f["status"] != "unreadable" else ""

    start, end = parse_period(value("period"))
    lines = []
    for i, row in enumerate(reading["rows"]):
        src = Source(upload, f"row:{i}", boxes_from["rows"][i]["row_bbox"])
        lines.append(
            Line(
                i,
                _row_date(row["date_text"], start, end),
                _signed(row),
                parse_amount(row["balance_text"]),
                row["description"],
                src,
                row.get("account_text", "").strip(),
            )
        )
    sources = {n: Source(upload, n, reading[n]["bbox"]) for n in (*KEY_FIELDS, "institution")}
    parsed = {
        "account_number": value("account_number").strip() or None,
        "period": start and end,
        "currency": parse_currency(value("currency")),
        "opening_balance": parse_amount(value("opening_balance")),
        "closing_balance": parse_amount(value("closing_balance")),
    }
    # "absent" is legitimate (page 2 of a statement has no opening); "unreadable" or unparseable is not.
    problems = [
        n
        for n in KEY_FIELDS
        if (reading[n]["status"] != "absent" or reading[n]["text"].strip()) and parsed[n] is None
    ]
    return Observation(
        upload=upload,
        institution=value("institution").strip(),
        currency=parsed["currency"],
        account_key=account_key(value("account_number")),
        account_number=value("account_number").strip(),
        holder=value("holder").strip(),
        period_start=start,
        period_end=end,
        opening=parsed["opening_balance"],
        closing=parsed["closing_balance"],
        lines=lines,
        sources=sources,
        problems=problems,
    )


def most_common(values) -> str | None:
    c = Counter(v for v in values if v)
    return c.most_common(1)[0][0] if c else None
