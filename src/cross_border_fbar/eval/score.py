"""Score one page reading against the answer key. Pure functions; raw counts, not adjectives."""

from __future__ import annotations

import re
from decimal import Decimal

from ..extract.parse import normalize_account_number, normalize_name, parse_amount, parse_currency, parse_date

FIELDS = ("institution", "currency", "account_number", "holder", "opening_balance", "closing_balance")


def iou(a: dict, b: dict) -> float:
    """a: model box (x_min...), b: truth box (x0...), both on the 0-1000 grid."""
    iw = max(0, min(a["x_max"], b["x1"]) - max(a["x_min"], b["x0"]))
    ih = max(0, min(a["y_max"], b["y1"]) - max(a["y_min"], b["y0"]))
    inter = iw * ih
    union = (
        (a["x_max"] - a["x_min"]) * (a["y_max"] - a["y_min"])
        + (b["x1"] - b["x0"]) * (b["y1"] - b["y0"])
        - inter
    )
    return inter / union if union > 0 else 0.0


def centre_in(a: dict, b: dict) -> bool:
    cx, cy = (a["x_min"] + a["x_max"]) / 2, (a["y_min"] + a["y_max"]) / 2
    return b["x0"] <= cx <= b["x1"] and b["y0"] <= cy <= b["y1"]


def printed_bank_names(field: dict) -> list[str]:
    """Every name the key says the page prints for its bank. A key that lists one name means just that."""
    return field.get("printed_names") or [field["value"]]


def reads_printed_name(text: str, names: list[str]) -> bool:
    """The text is one or more of the page's printed bank names and nothing else.

    A bilingual header can be returned in either language or both. Empty text is not a reading, and a name
    with anything added to it ("Siam Lotus Bank / Shinhan") is not what the page prints.
    """
    rest = normalize_name(text)
    wanted = [n for n in (normalize_name(name) for name in names) if n and n in rest]
    if not wanted:
        return False
    for name in sorted(wanted, key=len, reverse=True):
        rest = rest.replace(name, " ")
    return re.search(r"\w", rest) is None


def _field_ok(name: str, got: dict, want: dict) -> bool:
    if got["status"] != "read":
        return False
    value = want["value"]
    if name == "account_number":
        return normalize_account_number(got["text"]) == normalize_account_number(value)
    if name == "holder":
        return normalize_name(got["text"]) == normalize_name(value)
    if name == "institution":
        return reads_printed_name(got["text"], printed_bank_names(want))
    if name == "currency":  # "KRW", "₩" and "원" all mean won
        return parse_currency(got["text"]) == value
    amount = parse_amount(got["text"])
    return amount is not None and amount == Decimal(value)


def _signed_amount(row: dict) -> Decimal | None:
    amount = parse_amount(row["amount_text"])
    if amount is None or row["amount_column"] == "signed":
        return amount
    return -abs(amount) if row["amount_column"] == "debit" else abs(amount)


def _score_fields(statement: dict, reading: dict) -> dict:
    """Printed fields are scored on value and position; unprinted ones must be reported absent."""
    out = {}
    for name in FIELDS:
        want, got = statement["fields"].get(name), reading[name]
        if want is None:
            # Not printed in the header of this page. A unit repeated in a column header (금액(원)) is still
            # a correct currency reading for the document.
            repeated = name == "currency" and parse_currency(got["text"]) == statement.get("doc_currency")
            out[name] = {
                "want": None,
                "got": got["text"],
                "status": got["status"],
                "value_ok": got["status"] == "absent" or repeated,
                "centre_in_cell": None,
                "iou_text": None,
            }
            continue
        out[name] = {
            "want": want["value"],
            "got": got["text"],
            "status": got["status"],
            "value_ok": _field_ok(name, got, want),
            "centre_in_cell": centre_in(got["bbox"], want["bbox"]),
            "iou_text": round(iou(got["bbox"], want["text_bbox"]), 3),
        }
    return out


def _boxes(prefix: str, got: dict, want: dict, balance_printed: bool) -> dict:
    out = {
        f"{prefix}row_centre_in_row": centre_in(got["row_bbox"], want["row_bbox"]),
        f"{prefix}amount_iou_text": round(iou(got["amount_bbox"], want["amount_text_bbox"]), 3),
    }
    if balance_printed:
        out[f"{prefix}balance_iou_text"] = round(iou(got["balance_bbox"], want["balance_text_bbox"]), 3)
    return out


def _score_row(want: dict, got: dict, snapped: dict | None, year: int) -> dict:
    balance_printed = want["balance_bbox"] is not None
    balance = parse_amount(got["balance_text"])
    when = parse_date(got["date_text"], year)
    row = {
        "txn_id": want["txn_id"],
        "returned": True,
        "date_ok": when is not None and when.isoformat() == want["date"],
        "amount_ok": _signed_amount(got) == Decimal(want["amount"]),
        "balance_ok": balance == Decimal(want["balance_after"]) if balance_printed else balance is None,
        "account_ok": normalize_account_number(got.get("account_text", ""))
        == normalize_account_number(want.get("account_printed", "")),
        **_boxes("", got, want, balance_printed),
    }
    if snapped:
        row.update(_boxes("snapped_", snapped, want, balance_printed))
    return row


def score_page(statement: dict, reading: dict, snapped: dict | None) -> dict:
    """Score a single-page statement. Rows are matched in printed order."""
    year = int(statement["period"][:4])
    want_rows, got_rows = statement["transactions"], reading["rows"]
    snap_rows = snapped["rows"] if snapped else [None] * len(got_rows)
    rows = [
        _score_row(t, got_rows[i], snap_rows[i], year)
        if i < len(got_rows)
        else {"txn_id": t["txn_id"], "returned": False}
        for i, t in enumerate(want_rows)
    ]
    return {
        "statement_id": statement["statement_id"],
        "layout": statement["layout"],
        "fields": _score_fields(statement, reading),
        "rows": rows,
        "rows_expected": len(want_rows),
        "rows_returned": len(got_rows),
        "snapped": snapped is not None,
    }


def summarize(pages: list[dict], iou_ok: float = 0.5) -> dict:
    """Raw counts across pages, overall and per layout."""

    def counts(ps: list[dict]) -> dict:
        f = [v for p in ps for v in p["fields"].values()]
        r = [x for p in ps for x in p["rows"]]
        ret = [x for x in r if x["returned"]]
        bal = [x for x in ret if "balance_iou_text" in x]

        def frac(n, d):
            return f"{n}/{d}"

        out = {
            "pages": len(ps),
            "fields_value_ok": frac(sum(x["value_ok"] for x in f), len(f)),
            "fields_centre_in_cell": frac(
                sum(bool(x["centre_in_cell"]) for x in f if x["centre_in_cell"] is not None),
                sum(x["centre_in_cell"] is not None for x in f),
            ),
            "rows_returned": frac(len(ret), len(r)),
            "extra_rows": sum(max(0, p["rows_returned"] - p["rows_expected"]) for p in ps),
            "date_ok": frac(sum(x["date_ok"] for x in ret), len(r)),
            "amount_ok": frac(sum(x["amount_ok"] for x in ret), len(r)),
            "balance_ok": frac(sum(x["balance_ok"] for x in ret), len(r)),
            "row_account_ok": frac(sum(x["account_ok"] for x in ret), len(r)),
            "row_centre_in_row": frac(sum(x["row_centre_in_row"] for x in ret), len(r)),
            f"balance_iou_text>={iou_ok}": frac(sum(x["balance_iou_text"] >= iou_ok for x in bal), len(bal)),
        }
        snapped = [x for x in ret if "snapped_row_centre_in_row" in x]
        if snapped:
            sbal = [x for x in snapped if "snapped_balance_iou_text" in x]
            out["snapped_row_centre_in_row"] = frac(
                sum(x["snapped_row_centre_in_row"] for x in snapped), len(r)
            )
            out[f"snapped_balance_iou_text>={iou_ok}"] = frac(
                sum(x["snapped_balance_iou_text"] >= iou_ok for x in sbal), len(bal)
            )
        return out

    layouts = sorted({p["layout"] for p in pages})
    return {
        "all": counts(pages),
        "by_layout": {lay: counts([p for p in pages if p["layout"] == lay]) for lay in layouts},
    }
