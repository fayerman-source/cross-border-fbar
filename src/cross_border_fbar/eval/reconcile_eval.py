"""End-to-end check: model readings -> reconciliation -> compared with the answer key.

    python -m cross_border_fbar.eval.reconcile_eval --set out/synth [--model muse-spark-1.3]

Reads the page readings saved by the extraction eval (results/<model>_<set>.json),
re-snaps their boxes from the page images, reconciles them with intake facts,
and scores each account's maximum, its source statement, its USD value and its
flags. Intake (ownership) comes from the answer key here; in the product it
comes from the client call or the CPA.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from .. import rates
from ..extract.parse import account_key
from ..extract.snap import snap_rows
from ..reconcile.accounts import Intake, reconcile
from ..reconcile.fbar import fbar_totals
from ..reconcile.observe import observe

RESULTS = Path(__file__).resolve().parents[3] / "results"
SCORED_CODES = {
    "max_not_at_month_end",
    "possible_intraday_peak",
    "max_from_computed_balance",
    "missing_statement",
    "signature_authority_only",
    "institution_names_differ",
    "no_balance_anchor",
    "account_number_masked",
}

# Review flags that follow from a field read differently on some pages. When one of the flag's own source
# pages has that field scored as misread, the tool was right to raise it: it is counted as raised by a
# misread, not as a false positive.
FIELD_OF_FLAG = {"institution_names_differ": "institution", "currency_conflict": "currency"}


def _expected_flags(acct: dict) -> set[tuple[str, str]]:
    out = set()
    for f in acct["expected_flags"]:
        where = f["statement_id"].rsplit("_", 1)[1] if f["code"] == "missing_statement" else ""
        out.add((f["code"], where))
    return out


def _got_flags(result) -> set[tuple[str, str]]:
    return {(f.code, f.month or "") for f in result.flags}


def evaluate(set_dir: Path, model: str) -> dict:
    truth = json.loads((set_dir / "truth.json").read_text())
    extraction = json.loads((RESULTS / f"{model}_{set_dir.name}.json").read_text())
    upload_to_statement = {}
    observations = []
    misread = {
        (page["statement_id"], name)
        for page in extraction["pages"]
        for name, f in page.get("fields", {}).items()
        if not f.get("value_ok", True)
    }
    unread = {}
    for page in extraction["pages"]:
        st = next(s for s in truth["statements"] if s["statement_id"] == page["statement_id"])
        page_image = st["page_images"][page.get("page", 1) - 1]
        image = set_dir / page_image["file"]
        upload = page_image["file"]  # an upload name per page; reconciliation never parses it
        upload_to_statement[upload] = st["statement_id"]
        if "reading" not in page:
            unread[upload] = str(page.get("error") or "no reading for this page")
            continue
        observations.append(observe(page["reading"], upload, snap_rows(page["reading"], image)))

    intake = {
        account_key(a["account_number"]): Intake(a["ownership"], a["financial_interest"], a["account_number"])
        for a in truth["accounts"]
    }
    results = reconcile(
        observations,
        intake,
        rates.load(truth["tax_year"]),
        truth["tax_year"],
        truth["household"]["flag_threshold_usd"],
        unread,
    )
    by_key = {r.account_key: r for r in results}

    accounts, tp, fp, fn = [], 0, 0, 0
    for acct in truth["accounts"]:
        r = by_key.get(account_key(acct["account_number"]))
        if r is not None and not r.uploads:
            r = None  # a row for an intake account with no page read: the documents didn't find it
        want_max = acct["max_value"]
        want_flags = _expected_flags(acct)
        from_misreads = {
            f.code: sorted({upload_to_statement[src.upload] for src in f.sources})
            for f in (r.flags if r else [])
            if f.code in FIELD_OF_FLAG
            and f.code not in {c for c, _ in want_flags}
            and any((upload_to_statement[src.upload], FIELD_OF_FLAG[f.code]) in misread for src in f.sources)
        }
        got_flags = (
            {f for f in _got_flags(r) if f[0] in SCORED_CODES and f[0] not in from_misreads} if r else set()
        )
        extra_review = (
            sorted(
                {f.code for f in r.flags if f.severity == "review"}
                - {c for c, _ in want_flags}
                - set(from_misreads)
            )
            if r
            else []
        )
        tp += len(want_flags & got_flags)
        fp += len(got_flags - want_flags)
        fn += len(want_flags - got_flags)
        accounts.append(
            {
                "account_id": acct["account_id"],
                "found": r is not None,
                # An unknowable maximum is right only when the tool also reports none.
                "max_ok": bool(r)
                and (
                    r.max_value is None
                    if want_max is None
                    else bool(r.max_value and r.max_value.amount == Decimal(want_max["amount"]))
                ),
                "max_source_ok": bool(r)
                and (
                    r.max_value is None
                    if want_max is None
                    else bool(
                        r.max_value
                        and upload_to_statement.get(r.max_value.source.upload) == want_max["statement_id"]
                    )
                ),
                "usd_ok": bool(r and r.max_value_usd == acct["max_value_usd"]),
                "want": {"max": want_max["amount"] if want_max else None, "usd": acct["max_value_usd"]},
                "got": {
                    "max": str(r.max_value.amount) if r and r.max_value else None,
                    "usd": r.max_value_usd if r else None,
                },
                "flags_missed": sorted(want_flags - got_flags),
                "flags_extra": sorted(got_flags - want_flags),
                "unscored_review_flags": extra_review,
                "flags_from_misreads": from_misreads,
            }
        )

    totals = fbar_totals(results)
    n = len(truth["accounts"])
    return {
        "model": model,
        "summary": {
            "accounts_found": f"{sum(a['found'] for a in accounts)}/{n}",
            "max_value_ok": f"{sum(a['max_ok'] for a in accounts)}/{n}",
            "max_source_ok": f"{sum(a['max_source_ok'] for a in accounts)}/{n}",
            "usd_ok": f"{sum(a['usd_ok'] for a in accounts)}/{n}",
            "flags": {
                "true_positive": tp,
                "false_positive": fp,
                "missed": fn,
                "raised_by_misreads": sum(len(a["flags_from_misreads"]) for a in accounts),
            },
            "aggregate_usd": {
                "got": totals.aggregate_max_usd,
                "want": truth["household"]["aggregate_max_usd"],
            },
            "extra_accounts": sorted(
                set(by_key) - {account_key(a["account_number"]) for a in truth["accounts"]}
            ),
        },
        "accounts": accounts,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--set", type=Path, default=Path("out/synth"))
    p.add_argument("--model", default="muse-spark-1.3")
    a = p.parse_args()
    result = evaluate(a.set, a.model)
    out = RESULTS / f"{a.model}_{a.set.name}_reconcile.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(result["summary"], indent=1))
    for acct in result["accounts"]:
        if acct["flags_missed"] or acct["flags_extra"] or acct["unscored_review_flags"] or not acct["usd_ok"]:
            print("ISSUE", json.dumps(acct, ensure_ascii=False))
    print(f"wrote {out}")
    s = result["summary"]
    perfect = all(
        not (a["flags_missed"] or a["flags_extra"] or a["unscored_review_flags"])
        and a["max_ok"]
        and a["usd_ok"]
        for a in result["accounts"]
    )
    if not perfect or s["extra_accounts"] or s["aggregate_usd"]["got"] != s["aggregate_usd"]["want"]:
        raise SystemExit(1)  # a regression on the clean synthetic set fails CI


if __name__ == "__main__":
    main()
