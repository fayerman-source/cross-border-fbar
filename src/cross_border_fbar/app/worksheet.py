"""Build the reviewable FBAR worksheet from page readings, intake facts and the CPA's corrections.

Everything here is derived: model readings (recorded) go through observe -> snap -> reconcile.
The answer key is never read. Each number and flag carries its sources, so the UI can show
the exact line on the page image it came from.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from .. import rates
from ..extract.snap import snap_rows
from ..reconcile.accounts import AccountResult, Flag, Intake, reconcile, split_by_row_account
from ..reconcile.corrections import (
    BALANCE_FIELDS,
    as_read,
    corrected_reading,
    correction_key,
    field_ref,
)
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


def _without_unverified_boxes(reading: dict) -> dict:
    reading = copy.deepcopy(reading)
    for field in (
        "institution",
        "currency",
        "account_number",
        "holder",
        "period",
        "opening_balance",
        "closing_balance",
    ):
        reading[field]["bbox"] = None
    for row in reading["rows"]:
        for field in ("row_bbox", "amount_bbox", "balance_bbox"):
            row[field] = None
    return reading


def page_readings(inp: Inputs) -> dict[str, dict]:
    """Each read page's reading as the model returned it, by upload."""
    pages = {}
    for page in json.loads(inp.readings.read_text())["pages"]:
        if "reading" in page:
            image = page_image(inp.set_dir, page["statement_id"], int(page.get("page", 1)))
            pages[f"pages/{image.name}"] = page["reading"]
    return pages


def readings_view(inp: Inputs, corrections: dict) -> dict:
    """Every correctable value on each page as it was read, with its correction, if any, beside it."""

    def value(upload: str, reading: dict, field: str) -> dict:
        return {
            "field": field,
            "wp": wp_ref(Source(upload, field_ref(field), None)),
            **as_read(reading, field),
            "correction": corrections.get(correction_key(upload, field)),
        }

    def row_view(upload: str, reading: dict, i: int, row: dict) -> dict:
        return {
            "ref": f"row:{i}",
            "wp": wp_ref(Source(upload, f"row:{i}", None)),
            "date": row["date_text"],
            "description": row["description"],
            "amount": {**value(upload, reading, f"row:{i}:amount"), "column": row["amount_column"]},
            "balance": value(upload, reading, f"row:{i}:balance"),
        }

    return {
        upload: {
            "balances": [value(upload, reading, name) for name in BALANCE_FIELDS],
            "rows": [row_view(upload, reading, i, row) for i, row in enumerate(reading["rows"])],
        }
        for upload, reading in page_readings(inp).items()
    }


def load_observations(
    inp: Inputs, value_corrections: dict | None = None
) -> tuple[list, dict[str, str], set[str]]:
    """Observations from the pages that were read, and each page that wasn't (upload -> reason).

    Corrections apply here, before reconciliation. Boxes still come from the reading as returned, so
    correcting a value can't move where its line is drawn."""
    readings = json.loads(inp.readings.read_text())
    observations, unread, unverified = [], {}, set()
    by_page = defaultdict(list)
    for correction in (value_corrections or {}).values():
        by_page[correction["upload"]].append(correction)
    for page in readings["pages"]:
        image = page_image(inp.set_dir, page["statement_id"], int(page.get("page", 1)))
        upload = f"pages/{image.name}"  # the upload name the UI shows; never parsed for meaning
        if "reading" not in page:
            unread[upload] = str(page.get("error") or "no reading for this page")
            continue
        reading = page["reading"]
        corrected = corrected_reading(reading, by_page[upload])
        if page.get("capture") and not page["capture"].get("boxes_verified", False):
            # Model boxes are not evidence of grounding until checked. Keep the page accessible,
            # but do not draw an unverified highlight or snap an unseen photo to guessed lines.
            unverified.add(upload)
            observations.append(observe(_without_unverified_boxes(corrected), upload))
        else:
            observations.append(observe(corrected, upload, snap_rows(reading, image)))
    return observations, unread, unverified


def _effective_intake(document, ownership_overrides, interest_overrides):
    intake = dict(document.accounts)
    for key, ownership in (ownership_overrides or {}).items():
        stated = intake.get(key, Intake("unknown"))
        intake[key] = replace(stated, ownership=ownership)
    for key, correction in (interest_overrides or {}).items():
        stated = intake.get(key, Intake("unknown"))
        intake[key] = replace(stated, financial_interest=correction["value"])
    return intake


def _flag_unverified_locations(results, unverified):
    for result in results:
        for upload in sorted(set(result.uploads) & unverified):
            result.flags.append(
                Flag(
                    "photo_location_unverified",
                    "photo source location is unverified",
                    [Source(upload, "page", None)],
                )
            )


def _value_correction_views(corrections: dict, pages: list) -> dict:
    """Each correction with the account it belongs to and its source line."""
    owners = {}
    for page in pages:
        for name, source in page.sources.items():
            owners[(page.upload, name)] = (page.account_key, source)
        for part in split_by_row_account(page):
            for line in part.lines:
                owners[(page.upload, line.source.ref)] = (part.account_key, line.source)
    views = {}
    for key, correction in sorted(corrections.items()):
        account, source = owners[(correction["upload"], field_ref(correction["field"]))]
        views[key] = {
            **correction,
            "account": account or f"unidentified:{correction['upload']}",
            "source": _source(source),
        }
    return views


def build(
    inp: Inputs,
    ownership_overrides: dict[str, str] | None = None,
    threshold_usd: int = 1000,
    *,
    rate_overrides: dict | None = None,
    currency_overrides: dict | None = None,
    interest_overrides: dict | None = None,
    ownership_details: dict | None = None,
    value_corrections: dict | None = None,
) -> dict:
    intake_doc = IntakeDocument.from_dict(json.loads(inp.intake.read_text()))
    intake = _effective_intake(intake_doc, ownership_overrides, interest_overrides)
    year_rates = rates.load(inp.tax_year)
    observations, unread, unverified = load_observations(inp, value_corrections)
    corrections = _value_correction_views(value_corrections or {}, observations)
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
    _flag_unverified_locations(results, unverified)
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
        account = _account(r, year_rates, intake, acct_flags, account_rates.get(r.account_key), corrections)
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
        "value_corrections": list(corrections.values()),
        "totals": {
            "aggregate_usd": totals.aggregate_max_usd,
            "threshold_usd": FBAR_AGGREGATE_USD,
            "above_threshold": totals.above_aggregate_threshold,
            "crossing_depends_on_flags": totals.crossing_depends_on_flags,
            "threshold_review_reasons": totals.threshold_review_reasons,
            "accounts_without_value": totals.accounts_without_value,
            "unidentified_uploads": totals.unidentified_uploads,
        },
    }


def _figure_field(balance) -> str:
    """The value this figure shows: a page field, a line's printed balance, or a line's amount if computed."""
    ref = balance.source.ref
    if not ref.startswith("row:"):
        return ref
    return f"{ref}:{'balance' if balance.kind == 'printed' else 'amount'}"


def _balance_view(balance, corrections: dict) -> dict | None:
    if balance is None:
        return None
    return {
        "amount": str(balance.amount),
        "kind": balance.kind,
        "day": balance.day.isoformat() if balance.day else None,
        "source": _source(balance.source),
        "correction": corrections.get(correction_key(balance.source.upload, _figure_field(balance))),
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


def _account(
    r: AccountResult, year_rates: dict, intake: dict, flag_ids: list[str], alternate, corrections: dict
) -> dict:
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
        "max": _balance_view(r.max_value, corrections),
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
