"""Turn text copied from a statement into numbers and dates. Deterministic, no model.

The model is asked to copy amounts and dates exactly as printed; locale rules
live here, where they can be tested: 1.234,56 (German) vs 1,234.56 (Thai) vs
1,234 (won), and Buddhist-era years (2568 = 2025).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

BUDDHIST_ERA_OFFSET = 543


def parse_amount(text: str) -> Decimal | None:
    """Parse a printed amount; None if there is no number. A leading or trailing '-' is negative."""
    s = (text or "").strip()
    if not s:
        return None
    negative = s.startswith("-") or s.endswith("-") or s.startswith("(") and s.endswith(")")
    digits = re.sub(r"[^\d.,]", "", s)
    if not re.search(r"\d", digits):
        return None
    last_dot, last_comma = digits.rfind("."), digits.rfind(",")
    decimal_sep = None
    if last_dot >= 0 and last_comma >= 0:
        decimal_sep = "." if last_dot > last_comma else ","
    elif last_comma >= 0 and re.search(r",\d{2}$", digits):
        decimal_sep = ","
    elif last_dot >= 0 and re.search(r"\.\d{2}$", digits):
        decimal_sep = "."
    if decimal_sep:
        whole, _, frac = digits.rpartition(decimal_sep)
        normalized = re.sub(r"[.,]", "", whole) + "." + frac
    else:
        normalized = re.sub(r"[.,]", "", digits)
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_date(text: str, year_hint: int | None = None) -> date | None:
    """Parse YYYY.MM.DD, DD.MM.YYYY, DD/MM/YYYY (Gregorian or Buddhist era) or DD.MM. with a year hint."""
    s = (text or "").strip()
    m = re.fullmatch(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})\.?", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
        if m:
            d, mo, y = map(int, m.groups())
        else:
            m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?", s)
            if not m or year_hint is None:
                return None
            d, mo = map(int, m.groups())
            y = year_hint
    if y > 2400:
        y -= BUDDHIST_ERA_OFFSET
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def normalize_account_number(text: str) -> str:
    return re.sub(r"[\s\-]", "", text or "").upper()


def normalize_name(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()
