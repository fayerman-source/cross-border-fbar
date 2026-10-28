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


def _is_negative(s: str) -> bool:
    return s.startswith("-") or s.endswith("-") or (s.startswith("(") and s.endswith(")"))


def _decimal_separator(digits: str) -> str | None:
    """The decimal separator, if any. With both marks the last wins; alone, it needs exactly 2 decimals."""
    last_dot, last_comma = digits.rfind("."), digits.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        return "." if last_dot > last_comma else ","
    for sep in (",", "."):
        if re.search(rf"\{sep}\d{{2}}$", digits):
            return sep
    return None


def parse_amount(text: str) -> Decimal | None:
    """Parse a printed amount; None if there is no number. A leading or trailing '-' is negative."""
    s = (text or "").strip()
    digits = re.sub(r"[^\d.,]", "", s)
    if not re.search(r"\d", digits):
        return None
    sep = _decimal_separator(digits)
    if sep:
        whole, _, frac = digits.rpartition(sep)
        normalized = re.sub(r"[.,]", "", whole) + "." + frac
    else:
        normalized = re.sub(r"[.,]", "", digits)
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    return -value if _is_negative(s) else value


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


# ISO codes the tool can convert (Treasury labels in rates.py) plus USD. Anything else is "unknown",
# not guessed: "All amounts in KRW" must not read as ALL, and "HK$" must not read as USD.
KNOWN_CURRENCIES = {"KRW", "EUR", "THB", "INR", "USD"}
_UNAMBIGUOUS_UNITS = {"₩": "KRW", "원": "KRW", "€": "EUR", "฿": "THB", "₹": "INR"}


def parse_currency(text: str) -> str | None:
    t = (text or "").strip()
    for token in re.findall(r"[A-Z]{3}", t.upper()):
        if token in KNOWN_CURRENCIES:
            return token
    for unit, iso in _UNAMBIGUOUS_UNITS.items():
        if unit in t:
            return iso
    return None


def account_key(text: str) -> str:
    """Grouping key for an account number. Masked numbers (with *) can't be matched, so they stay apart."""
    norm = normalize_account_number(text)
    return f"masked:{norm}" if "*" in norm else norm
