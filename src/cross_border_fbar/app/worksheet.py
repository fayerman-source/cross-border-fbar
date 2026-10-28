"""Build the reviewable FBAR worksheet from page readings, intake facts and the CPA's corrections.

Everything here is derived: model readings (recorded) go through observe -> snap -> reconcile.
The answer key is never read. Each number and flag carries its sources, so the UI can show
the exact line on the page image it came from.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .. import rates
from ..extract.parse import account_key
from ..extract.snap import snap_rows
from ..reconcile.accounts import AccountResult, Flag, Intake, reconcile
from ..reconcile.fbar import FBAR_AGGREGATE_USD, fbar_totals
from ..reconcile.observe import Source, observe

SCOPE_LINE = "Balances, thresholds and review flags only. Not tax advice; every flag needs a CPA's review."


@dataclass
class Inputs:
    set_dir: Path
    readings: Path  # results/<model>_<set>.json from the extraction eval (recorded responses)
    intake: Path
    tax_year: int = 2025


def _source(src: Source) -> dict:
    return {"upload": src.upload, "ref": src.ref, "bbox": src.bbox, "wp": wp_ref(src)}


def wp_ref(src: Source) -> str:
    """A workpaper-style reference: the page it came from and the line on it."""
    page = Path(src.upload).stem.replace("_", " · ")
    line = f"r{int(src.ref.split(':')[1]) + 1}" if src.ref.startswith("row:") else src.ref.replace("_", " ")
    return f"{page} · {line}"


def flag_id(account: str, f: Flag) -> str:
    # The message carries what tells same-code flags apart when they have no month or sources
    # (e.g. two coverage gaps name their own date ranges), so each gets its own acknowledgement.
    raw = "|".join(
        [account, f.code, f.month or "", f.message, *sorted(f"{s.upload}#{s.ref}" for s in f.sources)]
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def page_image(set_dir: Path, statement_id: str, page: int) -> Path:
    """The page image for a reading, which must be a file directly inside the set's pages/ folder."""
    if not SAFE_ID.fullmatch(statement_id) or page < 1:
        raise ValueError(f"unexpected statement id {statement_id!r} in the readings file")
    pages = (set_dir / "pages").resolve()
    image = (pages / f"{statement_id}-p{page}.png").resolve()
    if image.parent != pages or not image.is_file():
        raise ValueError(f"no page image for {statement_id} page {page}")
    return image


def load_observations(inp: Inputs) -> list:
    readings = json.loads(inp.readings.read_text())
    observations = []
    for page in readings["pages"]:
        if "reading" not in page:
            continue
        image = page_image(inp.set_dir, page["statement_id"], int(page.get("page", 1)))
        upload = f"pages/{image.name}"  # the upload name the UI shows; never parsed for meaning
        observations.append(observe(page["reading"], upload, snap_rows(page["reading"], image)))
    return observations


def build(inp: Inputs, ownership_overrides: dict[str, str] | None = None, threshold_usd: int = 1000) -> dict:
    intake_doc = json.loads(inp.intake.read_text())
    intake = {
        account_key(a["account_number"]): Intake(a["ownership"], a.get("financial_interest"))
        for a in intake_doc["accounts"]
    }
    for key, ownership in (ownership_overrides or {}).items():
        intake[key] = Intake(ownership, ownership != "signature_authority")
    year_rates = rates.load(inp.tax_year)
    results = reconcile(load_observations(inp), intake, year_rates, inp.tax_year, threshold_usd)
    totals = fbar_totals(results)
    readings_meta = json.loads(inp.readings.read_text())
    accounts, flags = [], []
    for r in results:
        acct_flags = []
        for f in r.flags:
            fid = flag_id(r.account_key, f)
            acct_flags.append(fid)
            flags.append(
                {
                    "id": fid,
                    "account": r.account_key,
                    "code": f.code,
                    "severity": f.severity,
                    "message": f.message,
                    "month": f.month,
                    "extra_usd": f.extra_usd,
                    "sources": [_source(s) for s in f.sources],
                }
            )
        accounts.append(_account(r, year_rates, intake, acct_flags))
    return {
        "tax_year": inp.tax_year,
        "client": intake_doc.get("client"),
        "scope": SCOPE_LINE,
        "reader": {"model": readings_meta.get("model"), "prompt": readings_meta.get("prompt")},
        "rates": {
            c: {"units_per_usd": str(r.units_per_usd), "record_date": r.record_date, "source": r.source_url}
            for c, r in year_rates.items()
        },
        "accounts": accounts,
        "flags": flags,
        "totals": {
            "aggregate_usd": totals.aggregate_max_usd,
            "threshold_usd": FBAR_AGGREGATE_USD,
            "above_threshold": totals.above_aggregate_threshold,
            "crossing_depends_on_flags": totals.crossing_depends_on_flags,
            "accounts_without_value": totals.accounts_without_value,
            "unidentified_uploads": totals.unidentified_uploads,
        },
    }


def _account(r: AccountResult, year_rates: dict, intake: dict, flag_ids: list[str]) -> dict:
    rate = year_rates.get(r.currency or "")
    m = r.max_value
    return {
        "key": r.account_key,
        "account_number": r.account_number,
        "identified": r.identified,
        "institutions": r.institutions,
        "currency": r.currency,
        "holders": r.holders,
        "ownership": r.ownership,
        "ownership_source": "intake" if r.account_key in intake else "unknown",
        "max": None
        if m is None
        else {
            "amount": str(m.amount),
            "kind": m.kind,
            "day": m.day.isoformat() if m.day else None,
            "source": _source(m.source),
        },
        "rate": str(rate.units_per_usd) if rate else ("1" if r.currency == "USD" else None),
        "max_usd": r.max_value_usd,
        "year_end_balance": str(r.year_end_balance) if r.year_end_balance is not None else None,
        "uploads": r.uploads,
        "flags": flag_ids,
    }


def worksheet_hash(ws: dict) -> str:
    """Fingerprint of what the CPA signs: accounts, totals and flags (not UI state)."""
    core = {k: ws[k] for k in ("tax_year", "accounts", "flags", "totals", "rates")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()


def decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
