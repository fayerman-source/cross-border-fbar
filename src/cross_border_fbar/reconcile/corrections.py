"""A reviewer's corrections to numbers the model read. The reading itself is never changed.

A correction replaces the text reconciliation sees for one value, and everything after it runs as if the page
had read that way. So a correction clears a flag only by making the figures agree, and maxima, bounds and
totals recompute from it with no separate path.
"""

from __future__ import annotations

import copy
import re

from ..extract.parse import parse_amount
from .observe import signed_amount

BALANCE_FIELDS = ("opening_balance", "closing_balance")
_ROW_FIELD = re.compile(r"row:(0|[1-9]\d*):(amount|balance)", re.ASCII)


def correction_key(upload: str, field: str) -> str:
    return f"{upload}#{field}"


def field_ref(field: str) -> str:
    """The source reference a correctable field belongs to: its row, or the page field itself."""
    return field.rsplit(":", 1)[0] if field.startswith("row:") else field


def as_read(reading: dict, field: str) -> dict | None:
    """What the model read for this value, as text and as a number; None if the page has no such value."""
    if field in BALANCE_FIELDS:
        read = reading[field]
        text = read["text"] if read["status"] != "unreadable" else ""
        value = parse_amount(text)
    else:
        match = _ROW_FIELD.fullmatch(field)
        if not match or int(match[1]) >= len(reading["rows"]):
            return None
        row = reading["rows"][int(match[1])]
        if match[2] == "amount":
            text, value = row["amount_text"], signed_amount(row)
        else:
            text, value = row["balance_text"], parse_amount(row["balance_text"])
    return {"text": text, "value": None if value is None else str(value)}


def corrected_reading(reading: dict, corrections: list[dict]) -> dict:
    """A copy of one page's reading with corrected values in place; the given reading is left as it is."""
    if not corrections:
        return reading
    out = copy.deepcopy(reading)
    for correction in corrections:
        field, text = correction["field"], correction["text"]
        if field in BALANCE_FIELDS:
            out[field].update(text=text, status="read")
            continue
        match = _ROW_FIELD.fullmatch(field)
        row = out["rows"][int(match[1])]
        if match[2] == "amount":  # the reviewer states the sign, so it no longer depends on the column read
            row.update(amount_text=text, amount_column="signed")
        else:
            row["balance_text"] = text
    return out
