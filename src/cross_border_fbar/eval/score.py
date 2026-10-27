"""Score one page reading against the answer key. Pure functions; raw counts, not adjectives."""

from __future__ import annotations

from decimal import Decimal

from ..extract.parse import normalize_account_number, normalize_name, parse_amount, parse_date

FIELDS = ("account_number", "holder", "opening_balance", "closing_balance")


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


def _field_ok(name: str, got: dict, want) -> bool:
    if got["status"] != "read":
        return False
    if name == "account_number":
        return normalize_account_number(got["text"]) == normalize_account_number(want)
    if name == "holder":
        return normalize_name(got["text"]) == normalize_name(want)
    value = parse_amount(got["text"])
    return value is not None and value == Decimal(want)


def score_page(statement: dict, reading: dict, snapped: dict | None) -> dict:
    """Score a single-page statement. Rows are matched in printed order."""
    year = int(statement["period"][:4])
    fields = {}
    for name in FIELDS:
        want = statement["fields"][name]
        got = reading[name]
        fields[name] = {
            "want": want["value"],
            "got": got["text"],
            "status": got["status"],
            "value_ok": _field_ok(name, got, want["value"]),
            "centre_in_cell": centre_in(got["bbox"], want["bbox"]),
            "iou_text": round(iou(got["bbox"], want["text_bbox"]), 3),
        }

    rows = []
    want_rows, got_rows = statement["transactions"], reading["rows"]
    snap_rows = snapped["rows"] if snapped else None
    for i, t in enumerate(want_rows):
        if i >= len(got_rows):
            rows.append({"txn_id": t["txn_id"], "returned": False})
            continue
        g = got_rows[i]
        amount = parse_amount(g["amount_text"])
        if amount is not None and g["amount_column"] == "debit":
            amount = -abs(amount)
        elif amount is not None and g["amount_column"] == "credit":
            amount = abs(amount)
        balance_printed = t["balance_bbox"] is not None
        balance = parse_amount(g["balance_text"])
        row = {
            "txn_id": t["txn_id"],
            "returned": True,
            "date_ok": parse_date(g["date_text"], year) is not None
            and parse_date(g["date_text"], year).isoformat() == t["date"],
            "amount_ok": amount == Decimal(t["amount"]),
            "balance_ok": (balance == Decimal(t["balance_after"])) if balance_printed else balance is None,
            "row_centre_in_row": centre_in(g["row_bbox"], t["row_bbox"]),
            "amount_iou_text": round(iou(g["amount_bbox"], t["amount_text_bbox"]), 3),
        }
        if balance_printed:
            row["balance_iou_text"] = round(iou(g["balance_bbox"], t["balance_text_bbox"]), 3)
        if snap_rows:
            s = snap_rows[i]
            row["snapped_row_centre_in_row"] = centre_in(s["row_bbox"], t["row_bbox"])
            row["snapped_amount_iou_text"] = round(iou(s["amount_bbox"], t["amount_text_bbox"]), 3)
            if balance_printed:
                row["snapped_balance_iou_text"] = round(iou(s["balance_bbox"], t["balance_text_bbox"]), 3)
        rows.append(row)
    return {
        "statement_id": statement["statement_id"],
        "layout": statement["layout"],
        "fields": fields,
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
            "fields_centre_in_cell": frac(sum(x["centre_in_cell"] for x in f), len(f)),
            "rows_returned": frac(len(ret), len(r)),
            "extra_rows": sum(max(0, p["rows_returned"] - p["rows_expected"]) for p in ps),
            "date_ok": frac(sum(x["date_ok"] for x in ret), len(r)),
            "amount_ok": frac(sum(x["amount_ok"] for x in ret), len(r)),
            "balance_ok": frac(sum(x["balance_ok"] for x in ret), len(r)),
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
