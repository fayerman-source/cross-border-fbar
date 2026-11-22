"""Draft FinCEN Form 114 account schedule CSV from a signed worksheet, for preparer verification.

Every row and unresolved review limitation from the signed worksheet is carried into the CSV so a
preparer never sees figures detached from missing-statement, balance-anchor or classification flags.
Nothing here determines whether anyone files.
"""

from __future__ import annotations

import csv
import io
import re

DRAFT_NOTE = "Draft schedule for preparer review; not a filing determination"
_SIGNED_NUMBER = re.compile(r"-\d+(?:\.\d+)?")

COLUMNS = (
    "row_kind",
    "tax_year",
    "client",
    "fincen_114_part",
    "institutions",
    "account_number",
    "holders",
    "ownership",
    "financial_interest",
    "currency",
    "max_observed_balance",
    "max_balance_kind",
    "max_balance_date",
    "source_reference",
    "exchange_rate_units_per_usd",
    "exchange_rate_basis",
    "exchange_rate_date",
    "max_value_usd",
    "unresolved_review_flags",
    "preparer_acknowledgements",
    "open_determinations",
    "draft_status",
    "signoff_fingerprint",
)


def _safe_cell(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if text[0] in ("=", "+", "@", "\t", "\r") or (text[0] == "-" and not _SIGNED_NUMBER.fullmatch(text)):
        return "'" + text
    return text


def _fincen_part(account: dict) -> str:
    if not account["identified"]:
        return "Excluded (unidentified or masked)"
    ownership = account["ownership"]
    interest = account["financial_interest"]
    if ownership in ("sole", "joint") and interest is False:
        return "Unresolved (ownership and financial interest conflict)"
    if ownership == "signature_authority" and interest is False:
        return "Part IV (signature authority, no financial interest)"
    if ownership == "signature_authority" and interest is True:
        return "Unresolved (signature authority and financial interest both stated)"
    if ownership == "signature_authority" and interest is None:
        return "Unresolved (financial interest not stated)"
    if ownership == "sole" and interest is True:
        return "Part II (financial interest, separate)"
    if ownership == "joint" and interest is True:
        return "Part III (financial interest, joint)"
    return "Unresolved (classification incomplete)"


def _format_determination(item: dict) -> str:
    head = f"{item['label']}: {item['message']}" if item.get("label") else item["message"]
    parts = []
    if item.get("accounts"):
        parts.append("accounts=" + ",".join(item["accounts"]))
    if item.get("uploads"):
        parts.append("uploads=" + ",".join(item["uploads"]))
    return f"{head} ({'; '.join(parts)})" if parts else head


def export_fincen114_csv(exported: dict) -> str:
    """Render a signed export dict as a UTF-8 CSV with account rows and a worksheet summary row."""
    ws = exported["worksheet"]
    signoff_hash = exported["signoff"]["hash"]
    remaining_by_account: dict[str, list[dict]] = {}
    for item in exported["remaining_review_items"]:
        remaining_by_account.setdefault(item["account"], []).append(item)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(COLUMNS)

    for acct in ws["accounts"]:
        items = remaining_by_account.get(acct["key"], [])
        flag_text = "; ".join(f"{it['code']}: {it['message']}" for it in items)
        ack_text = "; ".join(
            f"{it['code']}: {it['acknowledgement']['note']} ({it['acknowledgement']['reviewer']})"
            for it in items
            if it.get("acknowledgement")
        )
        max_info = acct["max"]
        interest = "" if acct["financial_interest"] is None else str(acct["financial_interest"]).lower()
        source_ref = max_info["source"]["wp"] if max_info else "; ".join(acct["uploads"])
        row = (
            "account" if acct["identified"] else "excluded_unidentified",
            ws["tax_year"],
            ws["client"] or "",
            _fincen_part(acct),
            "; ".join(acct["institutions"]),
            acct["account_number"] or "; ".join(acct["uploads"]),
            "; ".join(acct["holders"]),
            acct["ownership"],
            interest,
            acct["currency"] or "",
            max_info["amount"] if max_info else "UNDETERMINED",
            max_info["kind"] if max_info else "",
            (max_info["day"] or "") if max_info else "",
            source_ref,
            acct["rate"] or "",
            acct["rate_basis"] or "",
            acct["rate_date"] or "",
            str(acct["max_usd"]) if acct["max_usd"] is not None else "UNDETERMINED",
            flag_text,
            ack_text,
            "",
            DRAFT_NOTE,
            signoff_hash,
        )
        writer.writerow([_safe_cell(cell) for cell in row])

    totals = ws["totals"]
    above = totals["above_threshold"]
    threshold_state = "undetermined" if above is None else ("above" if above else "not_above")
    determinations = "; ".join(_format_determination(d) for d in exported["open_determinations"])
    all_remaining = "; ".join(f"{it['account']}:{it['code']}" for it in exported["remaining_review_items"])
    summary_row = (
        "worksheet_summary",
        ws["tax_year"],
        ws["client"] or "",
        f"Aggregate vs ${totals['threshold_usd']:,} threshold: {threshold_state}",
        "",
        "",
        "",
        "",
        "",
        "USD",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        str(totals["aggregate_usd"]),
        all_remaining,
        "",
        determinations,
        DRAFT_NOTE,
        signoff_hash,
    )
    writer.writerow([_safe_cell(cell) for cell in summary_row])
    return buf.getvalue()
