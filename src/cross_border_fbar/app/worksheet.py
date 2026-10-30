"""Build the reviewable FBAR worksheet from page readings, intake facts and the CPA's corrections.

Everything here is derived: model readings (recorded) go through observe -> snap -> reconcile.
The answer key is never read. Each number and flag carries its sources, so the UI can show
the exact line on the page image it came from.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from .. import rates
from ..extract.snap import snap_rows
from ..reconcile.accounts import AccountResult, Flag, Intake, reconcile, split_by_row_account
from ..reconcile.fbar import FBAR_AGGREGATE_USD, fbar_totals
from ..reconcile.flags import CATALOG
from ..reconcile.intake import IntakeDocument
from ..reconcile.observe import Source, observe
from .form8938 import build_panel

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


def load_observations(inp: Inputs) -> tuple[list, dict[str, str]]:
    """Observations from the pages that were read, and each page that wasn't (upload -> reason)."""
    readings = json.loads(inp.readings.read_text())
    observations, unread = [], {}
    for page in readings["pages"]:
        image = page_image(inp.set_dir, page["statement_id"], int(page.get("page", 1)))
        upload = f"pages/{image.name}"  # the upload name the UI shows; never parsed for meaning
        if "reading" not in page:
            unread[upload] = str(page.get("error") or "no reading for this page")
            continue
        observations.append(observe(page["reading"], upload, snap_rows(page["reading"], image)))
    return observations, unread


def _effective_intake(document, ownership_overrides, interest_overrides):
    intake = dict(document.accounts)
    for key, ownership in (ownership_overrides or {}).items():
        stated = intake.get(key, Intake("unknown"))
        intake[key] = replace(stated, ownership=ownership)
    for key, correction in (interest_overrides or {}).items():
        stated = intake.get(key, Intake("unknown"))
        intake[key] = replace(stated, financial_interest=correction["value"])
    return intake


def build(
    inp: Inputs,
    ownership_overrides: dict[str, str] | None = None,
    threshold_usd: int = 1000,
    *,
    rate_overrides: dict | None = None,
    currency_overrides: dict | None = None,
    interest_overrides: dict | None = None,
    ownership_details: dict | None = None,
) -> dict:
    intake_doc = IntakeDocument.from_dict(json.loads(inp.intake.read_text()))
    intake = _effective_intake(intake_doc, ownership_overrides, interest_overrides)
    year_rates = rates.load(inp.tax_year)
    observations, unread = load_observations(inp)
    observations = [part for page in observations for part in split_by_row_account(page)]
    account_rates = {
        key: rates.Rate(v["currency"], Decimal(v["units_per_usd"]), v["record_date"], v["source"], "cpa")
        for key, v in (rate_overrides or {}).items()
    }
    selections = {key: v["currency"] for key, v in (currency_overrides or {}).items()}
    results = reconcile(
        observations,
        intake,
        year_rates,
        inp.tax_year,
        threshold_usd,
        unread,
        account_rates=account_rates,
        currency_selections=selections,
    )
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
                    "title": CATALOG[f.code].title,
                    "condition": CATALOG[f.code].condition,
                    "cpa_action": CATALOG[f.code].cpa_action,
                    "message": f.message,
                    "month": f.month,
                    "extra_usd": f.extra_usd,
                    "sources": [_source(s) for s in f.sources],
                }
            )
        account = _account(r, year_rates, intake, acct_flags, account_rates.get(r.account_key))
        if r.account_key in (ownership_overrides or {}):
            account["ownership_source"] = "review_correction"
        account["currency_correction"] = (currency_overrides or {}).get(r.account_key)
        account["observed_currencies"] = sorted(
            {o.currency for o in observations if o.account_key == r.account_key and o.currency}
        )
        account["currency_review_needed"] = any(
            observation.currency is None
            for observation in observations
            if observation.account_key == r.account_key
        )
        accounts.append(account)
    panel = build_panel(results, intake_doc, intake, _source)
    account_views = {account["key"]: account for account in accounts}
    for row in panel["accounts"]:
        account = account_views[row["key"]]
        row["rate"] = {
            "currency": account["currency"],
            "units_per_usd": account["rate"],
            "date": account["rate_date"],
            "source": account["rate_source"],
            "basis": account["rate_basis"],
        }
    panel["corrections"] = {
        "ownership": ownership_details or {},
        "financial_interest": interest_overrides or {},
    }
    return {
        "tax_year": inp.tax_year,
        "client": intake_doc.client,
        "form8938": panel,
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


def _balance_view(balance) -> dict | None:
    if balance is None:
        return None
    return {
        "amount": str(balance.amount),
        "kind": balance.kind,
        "day": balance.day.isoformat() if balance.day else None,
        "source": _source(balance.source),
    }


def _bounds_view(bounds) -> dict | None:
    if bounds is None:
        return None
    return {"low": str(bounds.low_usd), "high": decimal_str(bounds.high_usd)}


def _rate_view(currency: str | None, rate: rates.Rate | None) -> dict:
    if rate is not None:
        return {
            "rate": str(rate.units_per_usd),
            "rate_source": rate.source_url,
            "rate_date": rate.record_date,
            "rate_basis": rate.basis,
        }
    if currency == "USD":
        return {"rate": "1", "rate_source": None, "rate_date": None, "rate_basis": "identity"}
    return {"rate": None, "rate_source": None, "rate_date": None, "rate_basis": None}


def _account(r: AccountResult, year_rates: dict, intake: dict, flag_ids: list[str], alternate=None) -> dict:
    rate = year_rates.get(r.currency or "")
    if rate is None and alternate and alternate.currency == r.currency:
        rate = alternate
    return {
        "key": r.account_key,
        "account_number": r.account_number,
        "identified": r.identified,
        "institutions": r.institutions,
        "currency": r.currency,
        "holders": r.holders,
        "ownership": r.ownership,
        "financial_interest": r.financial_interest,
        "ownership_source": "intake" if r.account_key in intake else "unknown",
        "max": _balance_view(r.max_value),
        **_rate_view(r.currency, rate),
        "max_usd": r.max_value_usd,
        "year_end_balance": decimal_str(r.year_end_balance),
        "year_end_usd": r.year_end_usd,
        "annual_max_complete": r.annual_max_complete,
        "maximum_bounds_usd": _bounds_view(r.maximum_bounds),
        "year_end_bounds_usd": _bounds_view(r.year_end_bounds),
        "uploads": r.uploads,
        "flags": flag_ids,
    }


def worksheet_hash(ws: dict) -> str:
    """Fingerprint of what the CPA signs: the whole worksheet. Review state lives outside it, so no field
    is left out of the signature by a list someone has to keep current."""
    return hashlib.sha256(json.dumps(ws, sort_keys=True, default=str).encode()).hexdigest()


def decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
