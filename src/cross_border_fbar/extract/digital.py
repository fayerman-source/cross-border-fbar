"""Deterministic extraction for born-digital (text-layer) PDFs and structured CSV bank exports.

When a PDF page carries embedded text and a recognizable transaction table whose parsed figures satisfy
balance arithmetic, we emit a standard `extract.page.SCHEMA` reading directly with `0..1000` bounding
boxes and zero model API calls. Whenever a page has no text layer (scans/photos), an unfamiliar table
structure, or inconsistent balances, `read_digital_pdf_page` returns `None` so callers fall back to the
vision model rather than guessing.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from ..reconcile.accounts import split_by_row_account
from ..reconcile.observe import observe
from .page import FIELD_NAMES
from .parse import parse_amount, parse_currency, parse_date, parse_typed_amount

ZERO_BOX = {"x_min": 0, "y_min": 0, "x_max": 0, "y_max": 0}

_DOC_TITLES = frozenset(
    {
        "거래내역서",
        "거래내역정리",
        "kontoauszug",
        "statementofaccount",
        "accountstatement",
        "bankstatement",
    }
)
_FOOTER_RE = re.compile(r"synthetic test document|^\d+\s*/\s*\d+$", re.IGNORECASE)


def _clamp_box(x0: float, y0: float, x1: float, y1: float) -> dict[str, int]:
    lo_x = max(0, min(1000, round(x0)))
    lo_y = max(0, min(1000, round(y0)))
    hi_x = max(lo_x, min(1000, round(x1)))
    hi_y = max(lo_y, min(1000, round(y1)))
    return {"x_min": lo_x, "y_min": lo_y, "x_max": hi_x, "y_max": hi_y}


def _union_box(boxes: list[dict[str, int]]) -> dict[str, int]:
    if not boxes:
        return dict(ZERO_BOX)
    return {
        "x_min": min(b["x_min"] for b in boxes),
        "y_min": min(b["y_min"] for b in boxes),
        "x_max": max(b["x_max"] for b in boxes),
        "y_max": max(b["y_max"] for b in boxes),
    }


def _field(text: str = "", bbox: dict[str, int] | None = None) -> dict:
    clean = text.strip()
    if not clean:
        return {"text": "", "status": "absent", "bbox": dict(ZERO_BOX)}
    return {"text": clean, "status": "read", "bbox": bbox or dict(ZERO_BOX)}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _thai_consonants(text: str) -> str:
    return "".join(ch for ch in text if "\u0e01" <= ch <= "\u0e2e")


def _extract_lines(tp, w_pt: float, h_pt: float) -> list[list[tuple[str, float, float, float, float]]]:
    """Return visual lines, each a list of `(text, x0, y0, x1, y1)` spans on the 0..1000 top-left grid."""
    raw = tp.get_text_range()
    limit = min(len(raw), tp.count_chars())
    lines: list[list[tuple[str, float, float, float, float]]] = []
    pos = 0
    for segment in raw[:limit].splitlines(keepends=True):
        stripped = segment.rstrip("\r\n")
        if stripped:
            spans: list[list] = []
            cur: list | None = None
            for off, ch in enumerate(stripped):
                idx = pos + off
                if idx >= limit:
                    break
                left, bottom, right, top = tp.get_charbox(idx)
                if ch == " " and (right - left) <= 0.01:
                    continue
                x0 = left / w_pt * 1000.0
                x1 = right / w_pt * 1000.0
                y0 = (h_pt - top) / h_pt * 1000.0
                y1 = (h_pt - bottom) / h_pt * 1000.0
                if cur is None:
                    cur = [ch, x0, y0, x1, y1]
                elif x0 - cur[3] > 10.0:
                    if cur[0].strip():
                        spans.append(cur)
                    cur = [ch, x0, y0, x1, y1]
                else:
                    cur[0] += ch
                    cur[1] = min(cur[1], x0)
                    cur[2] = min(cur[2], y0)
                    cur[3] = max(cur[3], x1)
                    cur[4] = max(cur[4], y1)
            if cur is not None and cur[0].strip():
                spans.append(cur)
            cleaned = [(s[0].strip(), s[1], s[2], s[3], s[4]) for s in spans if s[0].strip()]
            if cleaned:
                lines.append(cleaned)
        pos += len(segment)
    return lines


def _column_role(label: str) -> str | None:
    n = _norm(label)
    th = _thai_consonants(label)
    if n in ("번호", "no", "no.", "#"):
        return "no"
    if (
        n in ("거래일자", "날짜", "buchungstag", "datum", "date", "transactiondate", "postingdate")
        or th == "วนท"
    ):
        return "date"
    if n in ("wert", "valuta", "valuedate"):
        return "value"
    if n in ("계좌번호", "accountnumber", "accountno", "iban"):
        return "account"
    if n in ("계좌", "종류", "channel"):
        return "meta_desc"
    if n in ("구분", "drcr", "dr/cr", "direction", "type"):
        return "direction"
    if (
        n
        in (
            "적요",
            "적요원문(상대방)",
            "buchungstext",
            "verwendungszweck",
            "รายการ",
            "description",
            "details",
            "narrative",
            "memo",
        )
        or "적요" in n
    ):
        return "desc"
    if n in ("비고", "remark", "remarks", "reference"):
        return "remark"
    if n in (
        "출금액",
        "출금",
        "soll",
        "ถอน",
        "debit",
        "debits",
        "withdrawal",
        "withdrawals",
        "paidout",
        "moneyout",
    ):
        return "debit"
    if n in (
        "입금액",
        "입금",
        "haben",
        "ฝาก",
        "credit",
        "credits",
        "deposit",
        "deposits",
        "paidin",
        "moneyin",
    ):
        return "credit"
    if n.startswith(("betrag", "금액", "amount")):
        return "amount"
    if n in ("잔액", "saldo", "คงเหลือ", "balance", "runningbalance"):
        return "balance"
    return None


def _find_table_header(
    lines: list[list[tuple[str, float, float, float, float]]],
) -> tuple[int, list[tuple[str, str, float, float, float, float]]] | None:
    for idx, spans in enumerate(lines):
        mapped = []
        for text, x0, y0, x1, y1 in spans:
            role = _column_role(text)
            if role is not None:
                mapped.append((role, text, x0, y0, x1, y1))
        roles = {r for r, *_ in mapped}
        if "date" in roles and ("amount" in roles or ("debit" in roles and "credit" in roles)):
            return idx, mapped
    return None


def _metadata_role(label: str) -> str | None:
    n = _norm(label).rstrip(":")
    th = _thai_consonants(label)
    if n in ("예금주", "kontoinhaber", "accountholder", "holder", "customername") or th in ("ชบญช", "ชอบญช"):
        return "holder"
    if n in ("계좌번호", "iban", "accountnumber", "accountno", "account") or th == "ลขทบญช":
        return "account_number"
    if n in ("조회기간", "zeitraum", "period", "statementperiod", "รอบรายการ") or th == "รอบรายการ":
        return "period"
    if n in ("통화", "금액단위", "währung", "waehrung", "currency") or th == "สกลงน":
        return "currency"
    if (
        any(
            k in n
            for k in ("전월이월잔액", "alterkontostand", "openingbalance", "beginningbalance", "balanceb/f")
        )
        or "ยอดยกมา" in label
    ):
        return "opening_balance"
    if (
        any(k in n for k in ("기말잔액", "neuerkontostand", "closingbalance", "endingbalance", "balancec/f"))
        or "ยอดยกไป" in label
    ):
        return "closing_balance"
    return None


def _first_amount_right_of(
    spans: list[tuple[str, float, float, float, float]], x_after: float
) -> tuple[str, dict[str, int]] | None:
    for text, x0, y0, x1, y1 in spans:
        if x0 >= x_after - 2.0 and parse_amount(text) is not None:
            return text, _clamp_box(x0, y0, x1, y1)
    return None


def _assign_column(
    span: tuple[str, float, float, float, float],
    columns: list[tuple[str, str, float, float, float, float]],
) -> str:
    text, sx0, _, sx1, _ = span
    s_mid = (sx0 + sx1) / 2.0
    is_num = parse_amount(text) is not None and parse_date(text) is None
    best_role = columns[0][0]
    best_dist = float("inf")
    for role, _, hx0, _, hx1, _ in columns:
        if role in ("debit", "credit", "amount", "balance"):
            if not is_num:
                continue
            # Numeric columns are right-aligned in bank tables; compare right edges as well as centres.
            dist = min(abs(sx1 - hx1), abs(s_mid - (hx0 + hx1) / 2.0))
        elif role == "desc":
            dist = (
                0.0
                if hx0 - 5.0 <= sx0 <= hx1 + 120.0
                else min(abs(sx0 - hx0), abs(s_mid - (hx0 + hx1) / 2.0))
            )
        else:
            dist = 0.0 if hx0 - 15.0 <= s_mid <= hx1 + 15.0 else abs(s_mid - (hx0 + hx1) / 2.0)
        if dist < best_dist:
            best_dist = dist
            best_role = role
    return best_role


def _direction_to_column(text: str) -> str | None:
    n = _norm(text)
    if n in ("출금", "출금액", "지급", "debit", "dr", "withdrawal", "out"):
        return "debit"
    if n in ("입금", "입금액", "credit", "cr", "deposit", "in"):
        return "credit"
    return None


def _part_balances_reconcile(part) -> bool:
    running: Decimal | None = part.opening
    for line in part.lines:
        if line.amount is None or line.day is None:
            return False
        if running is not None:
            running += line.amount
            if line.printed_balance is not None and running != line.printed_balance:
                return False
        elif line.printed_balance is not None:
            running = line.printed_balance
    return not (running is not None and part.closing is not None and running != part.closing)


def _passes_reconciliation_guard(reading: dict) -> bool:
    """Reject a digital parse if any field/row failed to parse or if per-account balances disagree."""
    obs = observe(reading, "digital-check")
    if obs.problems:
        return False
    parts = split_by_row_account(obs)
    if len(parts) > 1 and not obs.account_key and (obs.opening is not None or obs.closing is not None):
        return False
    return all(_part_balances_reconcile(part) for part in parts)


def read_digital_pdf_page(pdf_path: Path, page: int = 1) -> dict | None:
    """Parse one born-digital PDF page into a `SCHEMA` reading, or return `None` to fall back to vision."""
    if page < 1:
        raise ValueError("PDF page must be at least 1")
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        if page > len(doc):
            raise ValueError("PDF page is outside the document")
        pg = doc[page - 1]
        try:
            w_pt, h_pt = pg.get_size()
            tp = pg.get_textpage()
            try:
                if tp.count_chars() < 20 or w_pt <= 0 or h_pt <= 0:
                    return None
                lines = _extract_lines(tp, w_pt, h_pt)
            finally:
                tp.close()
        finally:
            pg.close()
    finally:
        doc.close()

    found = _find_table_header(lines)
    if found is None:
        return None
    header_idx, columns = found
    col_roles = {r for r, *_ in columns}
    table_x0 = min(c[2] for c in columns)
    table_x1 = max(c[4] for c in columns)

    fields = {name: _field() for name in FIELD_NAMES}

    # First top-left heading above metadata, unless it is a generic document title.
    if header_idx > 0:
        first_text, fx0, fy0, fx1, fy1 = lines[0][0]
        if fx0 < 200 and _norm(first_text) not in _DOC_TITLES and _metadata_role(first_text) is None:
            fields["institution"] = _field(first_text, _clamp_box(fx0, fy0, fx1, fy1))

    for spans in lines[:header_idx]:
        for idx, (text, _x0, _y0, x1, _y1) in enumerate(spans):
            role = _metadata_role(text)
            if role == "opening_balance":
                amt = _first_amount_right_of(spans[idx + 1 :], x1)
                if amt:
                    fields["opening_balance"] = _field(amt[0], amt[1])
            elif role in ("holder", "account_number", "period", "currency") and idx + 1 < len(spans):
                val_spans = []
                for vtext, vx0, vy0, vx1, vy1 in spans[idx + 1 :]:
                    if _metadata_role(vtext) is not None:
                        break
                    val_spans.append((vtext, vx0, vy0, vx1, vy1))
                if val_spans:
                    joined = " ".join(v[0] for v in val_spans)
                    vbox = _union_box([_clamp_box(v[1], v[2], v[3], v[4]) for v in val_spans])
                    fields[role] = _field(joined, vbox)

    if fields["currency"]["status"] == "absent":
        for role, htext, hx0, hy0, hx1, hy1 in columns:
            if role == "amount":
                m = re.search(r"\(([^)]+)\)|\b([A-Z]{3})\b", htext)
                token = (m.group(1) or m.group(2)) if m else ""
                if token and parse_currency(token) is not None:
                    fields["currency"] = _field(token, _clamp_box(hx0, hy0, hx1, hy1))
                    break

    rows: list[dict] = []
    for spans in lines[header_idx + 1 :]:
        joined_line = " ".join(s[0] for s in spans)
        if _FOOTER_RE.search(joined_line) or all(_FOOTER_RE.search(s[0]) for s in spans):
            continue
        if all(s[2] >= 940.0 for s in spans) and not any(parse_date(s[0]) for s in spans):
            continue
        first_role = _metadata_role(spans[0][0])
        if first_role == "opening_balance":
            amt = _first_amount_right_of(spans[1:], spans[0][3])
            if amt:
                fields["opening_balance"] = _field(amt[0], amt[1])
            continue
        if first_role == "closing_balance":
            amt = _first_amount_right_of(spans[1:], spans[0][3])
            if amt:
                fields["closing_balance"] = _field(amt[0], amt[1])
            break

        # Repeated table header on a page break: skip cleanly.
        if {r for r in (_column_role(s[0]) for s in spans) if r} >= {"date"} and len(spans) >= 4:
            if any(_column_role(s[0]) in ("amount", "debit", "credit") for s in spans):
                continue

        by_role: dict[str, list[tuple[str, dict[str, int]]]] = {}
        for span in spans:
            role = _assign_column(span, columns)
            by_role.setdefault(role, []).append((span[0], _clamp_box(span[1], span[2], span[3], span[4])))

        date_items = by_role.get("date") or []
        if not date_items:
            return None
        date_text = date_items[0][0]

        amount_text = ""
        amount_bbox = dict(ZERO_BOX)
        amount_column = "signed"
        if "debit" in col_roles and "credit" in col_roles:
            debits = by_role.get("debit") or []
            credits = by_role.get("credit") or []
            if bool(debits) == bool(credits):
                return None
            if debits:
                amount_text, amount_bbox = debits[0]
                amount_column = "debit"
            else:
                amount_text, amount_bbox = credits[0]
                amount_column = "credit"
        else:
            amts = by_role.get("amount") or []
            if len(amts) != 1:
                return None
            amount_text, amount_bbox = amts[0]
            if "direction" in col_roles:
                dirs = by_role.get("direction") or []
                if not dirs:
                    return None
                mapped_dir = _direction_to_column(dirs[0][0])
                if mapped_dir is None:
                    return None
                amount_column = mapped_dir

        balances = by_role.get("balance") or []
        balance_text = balances[0][0] if balances else ""
        balance_bbox = balances[0][1] if balances else dict(ZERO_BOX)

        accounts = by_role.get("account") or []
        account_text = accounts[0][0] if accounts else ""

        desc_parts = [item[0] for item in (by_role.get("desc") or [])]
        if not desc_parts and by_role.get("remark"):
            desc_parts = [item[0] for item in by_role["remark"]]
        description = " ".join(desc_parts).strip()

        row_y0 = min(s[2] for s in spans)
        row_y1 = max(s[4] for s in spans)
        row_x0 = min(table_x0, min(s[1] for s in spans))
        row_x1 = max(table_x1, max(s[3] for s in spans))
        rows.append(
            {
                "date_text": date_text,
                "account_text": account_text,
                "description": description,
                "amount_text": amount_text,
                "amount_column": amount_column,
                "balance_text": balance_text,
                "row_bbox": _clamp_box(row_x0, row_y0, row_x1, row_y1),
                "amount_bbox": amount_bbox,
                "balance_bbox": balance_bbox,
            }
        )

    reading = {**fields, "rows": rows}
    if not _passes_reconciliation_guard(reading):
        return None
    return reading


_CSV_HEADER_MAP = {
    "date": "date",
    "transactiondate": "date",
    "postingdate": "date",
    "buchungstag": "date",
    "datum": "date",
    "거래일자": "date",
    "날짜": "date",
    "description": "description",
    "details": "description",
    "memo": "description",
    "narrative": "description",
    "buchungstext": "description",
    "verwendungszweck": "description",
    "적요": "description",
    "amount": "amount",
    "betrag": "amount",
    "금액": "amount",
    "금액(원)": "amount",
    "debit": "debit",
    "withdrawal": "debit",
    "withdrawals": "debit",
    "soll": "debit",
    "출금액": "debit",
    "출금": "debit",
    "credit": "credit",
    "deposit": "credit",
    "deposits": "credit",
    "haben": "credit",
    "입금액": "credit",
    "입금": "credit",
    "direction": "direction",
    "type": "direction",
    "drcr": "direction",
    "dr/cr": "direction",
    "구분": "direction",
    "balance": "balance",
    "runningbalance": "balance",
    "saldo": "balance",
    "잔액": "balance",
    "account": "account_number",
    "accountnumber": "account_number",
    "accountno": "account_number",
    "iban": "account_number",
    "계좌번호": "account_number",
    "currency": "currency",
    "통화": "currency",
    "währung": "currency",
    "institution": "institution",
    "bank": "institution",
    "bankname": "institution",
    "holder": "holder",
    "accountholder": "holder",
    "예금주": "holder",
    "period": "period",
    "statementperiod": "period",
    "조회기간": "period",
}


_CSV_ONE_DEC_RE = re.compile(
    r"(?:\d+|\d{1,3}(?:,\d{3})+)\.\d|(?:\d+|\d{1,3}(?:\.\d{3})+),\d",
    re.ASCII,
)


def _strip_sign_affix(s: str) -> tuple[str, str, str]:
    if s.startswith("(") and s.endswith(")"):
        return "(", s[1:-1].strip(), ")"
    if s.startswith(("+", "-")):
        return ("-" if s[0] == "-" else ""), s[1:].strip(), ""
    if s.endswith("-"):
        return "", s[:-1].strip(), "-"
    return "", s, ""


def _normalize_csv_number(raw: str) -> tuple[str, Decimal] | None:
    """Validate strict CSV numeric syntax and return `(canonical_text, value)` agreed on by `parse_amount`."""
    s = (raw or "").strip()
    if not s:
        return None
    pre, body, suf = _strip_sign_affix(s)
    clean = f"{pre}{body}{suf}"
    typed = parse_typed_amount(clean)
    if typed is not None and parse_amount(clean) == typed:
        return clean, typed
    if _CSV_ONE_DEC_RE.fullmatch(body):
        padded = f"{pre}{body}0{suf}"
        val = parse_amount(padded)
        return (padded, val) if val is not None else None
    return None


def _parse_csv_amount_cell(cell, mapped_cols: dict[str, int], r_i: int) -> tuple[str, str]:
    if "debit" in mapped_cols and "credit" in mapped_cols:
        deb = cell("debit")
        cred = cell("credit")
        deb_norm = _normalize_csv_number(deb) if deb else None
        cred_norm = _normalize_csv_number(cred) if cred else None
        if (deb and deb_norm is None) or (cred and cred_norm is None):
            raise ValueError(f"CSV row {r_i} has an unparseable debit or credit figure")
        if deb_norm and cred_norm and deb_norm[1] and cred_norm[1]:
            raise ValueError(f"CSV row {r_i} has both debit and credit populated")
        if deb_norm and deb_norm[1]:
            return deb_norm[0], "debit"
        if cred_norm and cred_norm[1]:
            return cred_norm[0], "credit"
        if deb_norm is not None:
            return deb_norm[0], "debit"
        if cred_norm is not None:
            return cred_norm[0], "credit"
        raise ValueError(f"CSV row {r_i} has no transaction amount")

    raw_amt = cell("amount")
    amt_norm = _normalize_csv_number(raw_amt)
    if amt_norm is None:
        raise ValueError(f"CSV row {r_i} has an unparseable amount {raw_amt!r}")
    if "direction" in mapped_cols and cell("direction"):
        mapped_dir = _direction_to_column(cell("direction"))
        if mapped_dir is None:
            raise ValueError(f"CSV row {r_i} has unrecognized direction {cell('direction')!r}")
        return amt_norm[0], mapped_dir
    return amt_norm[0], "signed"


def _validate_csv_meta_balances(meta: dict[str, str]) -> None:
    for bal_field in ("opening_balance", "closing_balance"):
        if meta.get(bal_field):
            norm_bal = _normalize_csv_number(meta[bal_field])
            if norm_bal is None:
                raise ValueError(f"CSV {bal_field} has an unparseable amount {meta[bal_field]!r}")
            meta[bal_field] = norm_bal[0]


def read_csv_statement(csv_path: Path) -> dict:
    """Parse a structured UTF-8 CSV bank statement into a `SCHEMA` reading.

    Raises `ValueError` rather than guessing when column headers, dates, amounts, or balances are
    missing, ambiguous, or inconsistent.
    """
    text = csv_path.read_text(encoding="utf-8").lstrip("\ufeff")
    raw_rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not raw_rows:
        raise ValueError("CSV statement is empty")

    meta: dict[str, str] = {}
    header_idx: int | None = None
    mapped_cols: dict[str, int] = {}

    for idx, row in enumerate(raw_rows):
        candidate: dict[str, int] = {}
        for col_i, cell in enumerate(row):
            role = _CSV_HEADER_MAP.get(_norm(cell).replace("_", ""))
            if role and role not in candidate:
                candidate[role] = col_i
        if "date" in candidate and (
            "amount" in candidate or ("debit" in candidate and "credit" in candidate)
        ):
            header_idx = idx
            mapped_cols = candidate
            break
        if len(row) >= 2:
            mrole = _metadata_role(row[0])
            if mrole:
                meta[mrole] = row[1].strip()
            elif _norm(row[0]).rstrip(":") in ("institution", "bank", "bankname"):
                meta["institution"] = row[1].strip()

    if header_idx is None:
        raise ValueError("CSV statement has no recognizable date and amount header row")
    _validate_csv_meta_balances(meta)

    data_rows = raw_rows[header_idx + 1 :]
    parsed_rows: list[dict] = []
    col_values: dict[str, set[str]] = {
        "institution": set(),
        "currency": set(),
        "holder": set(),
        "period": set(),
    }
    row_accounts: list[str] = []

    for r_i, row in enumerate(data_rows, start=1):

        def cell(role: str, row: list[str] = row) -> str:
            pos = mapped_cols.get(role)
            return row[pos].strip() if pos is not None and pos < len(row) else ""

        date_text = cell("date")
        if not date_text:
            raise ValueError(f"CSV row {r_i} is missing a date")

        amount_text, amount_column = _parse_csv_amount_cell(cell, mapped_cols, r_i)

        raw_bal = cell("balance")
        if raw_bal:
            bal_norm = _normalize_csv_number(raw_bal)
            if bal_norm is None:
                raise ValueError(f"CSV row {r_i} has an unparseable balance {raw_bal!r}")
            balance_text = bal_norm[0]
        else:
            balance_text = ""

        acct_cell = cell("account_number")
        if acct_cell:
            row_accounts.append(acct_cell)
        for key in col_values:
            val = cell(key)
            if val:
                col_values[key].add(val)

        parsed_rows.append(
            {
                "date_text": date_text,
                "account_text": acct_cell,
                "description": cell("description"),
                "amount_text": amount_text,
                "amount_column": amount_column,
                "balance_text": balance_text,
                "row_bbox": dict(ZERO_BOX),
                "amount_bbox": dict(ZERO_BOX),
                "balance_bbox": dict(ZERO_BOX),
            }
        )

    raw_currencies = set(col_values["currency"])
    if meta.get("currency"):
        raw_currencies.add(meta["currency"])
    parsed_currencies = {parse_currency(c) for c in raw_currencies}
    if None in parsed_currencies:
        raise ValueError("CSV statement contains an unrecognized currency")
    if len(parsed_currencies) > 1 or len(col_values["currency"]) > 1:
        raise ValueError("CSV statement mixes multiple currencies; split by currency before import")

    fields = {name: _field(meta.get(name, "")) for name in FIELD_NAMES}
    for key, seen in col_values.items():
        if fields[key]["status"] == "absent" and len(seen) == 1:
            fields[key] = _field(next(iter(seen)))

    unique_accts = set(row_accounts)
    if fields["account_number"]["status"] == "absent" and len(unique_accts) == 1:
        only_acct = next(iter(unique_accts))
        fields["account_number"] = _field(only_acct)
        for r in parsed_rows:
            r["account_text"] = ""

    if fields["period"]["status"] == "absent" and parsed_rows:
        parsed_days = [parse_date(r["date_text"]) for r in parsed_rows]
        if all(d is not None for d in parsed_days):
            lo, hi = min(parsed_days), max(parsed_days)
            fields["period"] = _field(f"{lo.isoformat()} ~ {hi.isoformat()}")

    reading = {**fields, "rows": parsed_rows}
    if not _passes_reconciliation_guard(reading):
        raise ValueError("CSV statement failed field or balance reconciliation checks")
    return reading


def render_csv_preview(reading: dict, out_png: Path) -> dict:
    """Render a clean tabular PNG preview of a CSV reading and populate matching 0..1000 row boxes."""
    row_h = 26
    top_h = 140
    n_rows = len(reading["rows"])
    width = 1000
    height = max(400, top_h + (n_rows + 2) * row_h + 40)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    y = 18
    draw.text((28, y), "CSV STATEMENT IMPORT PREVIEW (SYNTHETIC)", fill=(40, 40, 40))
    y += 24
    updated = {**reading, "rows": []}
    for name in FIELD_NAMES:
        val = reading[name]["text"]
        if val:
            y0, y1 = y, y + 16
            draw.text((28, y0), f"{name}: {val}", fill=(20, 20, 20))
            updated[name] = {
                **reading[name],
                "bbox": _clamp_box(28, y0 / height * 1000, 700, y1 / height * 1000),
            }
            y += 16

    y = max(y + 12, top_h)
    draw.rectangle((24, y, 976, y + row_h - 4), fill=(235, 235, 235))
    draw.text((30, y + 4), "Date", fill=(20, 20, 20))
    draw.text((150, y + 4), "Description", fill=(20, 20, 20))
    draw.text((470, y + 4), "Direction", fill=(20, 20, 20))
    draw.text((560, y + 4), "Amount", fill=(20, 20, 20))
    draw.text((760, y + 4), "Balance", fill=(20, 20, 20))
    y += row_h

    for row in reading["rows"]:
        y0, y1 = y, y + row_h - 4
        draw.text((30, y0 + 4), row["date_text"][:18], fill=(20, 20, 20))
        draw.text((150, y0 + 4), row["description"][:42], fill=(20, 20, 20))
        draw.text((470, y0 + 4), row["amount_column"].upper(), fill=(20, 20, 20))
        draw.text((560, y0 + 4), row["amount_text"], fill=(20, 20, 20))
        if row["balance_text"]:
            draw.text((760, y0 + 4), row["balance_text"], fill=(20, 20, 20))
        rbox = _clamp_box(24, y0 / height * 1000, 976, y1 / height * 1000)
        abox = _clamp_box(465, y0 / height * 1000, 735, y1 / height * 1000)
        bbox = (
            _clamp_box(755, y0 / height * 1000, 960, y1 / height * 1000)
            if row["balance_text"]
            else dict(ZERO_BOX)
        )
        updated["rows"].append({**row, "row_bbox": rbox, "amount_bbox": abox, "balance_bbox": bbox})
        y += row_h

    out_png.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_png, format="PNG")
    return updated
