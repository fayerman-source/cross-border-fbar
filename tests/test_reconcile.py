"""Reconciliation with no model: perfect readings from the generator must reproduce the answer key."""

import copy
from decimal import Decimal

import pytest

from cross_border_fbar import rates
from cross_border_fbar.extract.parse import account_key, normalize_account_number
from cross_border_fbar.reconcile.accounts import Intake, reconcile
from cross_border_fbar.reconcile.fbar import fbar_totals
from cross_border_fbar.reconcile.observe import observe, parse_currency, parse_period
from cross_border_fbar.synth.__main__ import TAX_YEAR, generate

BOX = {"x_min": 0, "y_min": 0, "x_max": 0, "y_max": 0}


@pytest.fixture(scope="module")
def truth(tmp_path_factory):
    return generate(tmp_path_factory.mktemp("synth"), 2025, png=False, dpi=150, threshold_usd=1000)


def reading_for(st: dict) -> dict:
    """What a flawless model would return for one page of a document, in the extraction schema."""
    f = st["fields"]
    names = (
        "institution",
        "currency",
        "account_number",
        "holder",
        "period",
        "opening_balance",
        "closing_balance",
    )

    def field(name):
        if name not in f:
            return {"text": "", "status": "absent", "bbox": BOX}
        return {"text": str(f[name]["value"]), "status": "read", "bbox": BOX}

    rows = []
    for t in st["transactions"]:
        debit = t["amount"].startswith("-")
        single_column = st["layout"] == "de_plain"
        rows.append(
            {
                "date_text": t["date"],
                "description": t["description"],
                "account_text": t.get("account_printed", ""),
                "amount_text": t["amount"] if single_column else t["amount"].lstrip("-"),
                "amount_column": "signed" if single_column else ("debit" if debit else "credit"),
                "balance_text": t["balance_after"] if t["balance_printed"] else "",
                "row_bbox": BOX,
                "amount_bbox": BOX,
                "balance_bbox": BOX,
            }
        )
    return {**{n: field(n) for n in names}, "rows": rows}


def intake_from(truth):
    return {
        normalize_account_number(a["account_number"]): Intake(a["ownership"], a["financial_interest"])
        for a in truth["accounts"]
    }


def run(truth, statements):
    obs = [observe(reading_for(st), st["statement_id"]) for st in statements]
    return {r.account_key: r for r in reconcile(obs, intake_from(truth), rates.load(TAX_YEAR), TAX_YEAR)}


def flag_set(result):
    return {(f.code, f.month or "") for f in result.flags}


def expected_set(acct):
    return {
        (f["code"], f["statement_id"].rsplit("_", 1)[1] if f["code"] == "missing_statement" else "")
        for f in acct["expected_flags"]
    }


def test_perfect_readings_reproduce_the_answer_key(truth):
    got = run(truth, truth["statements"])
    for acct in truth["accounts"]:
        r = got[account_key(acct["account_number"])]
        if acct["max_value"] is None:
            assert r.max_value is None, acct["account_id"]
            assert r.max_value_usd is None
        else:
            assert r.max_value.amount == Decimal(acct["max_value"]["amount"]), acct["account_id"]
            assert r.max_value.source.upload == acct["max_value"]["statement_id"]
            assert r.max_value_usd == acct["max_value_usd"]
        assert flag_set(r) == expected_set(acct), acct["account_id"]
    assert fbar_totals(list(got.values())).aggregate_max_usd == truth["household"]["aggregate_max_usd"]


def test_a_duplicate_upload_is_counted_once(truth):
    sts = truth["statements"] + [truth["statements"][2]]
    r = run(truth, sts)[normalize_account_number(truth["accounts"][0]["account_number"])]
    assert len(r.duplicate_uploads) == 1


def test_a_misread_balance_is_caught_by_arithmetic(truth):
    st = copy.deepcopy(next(s for s in truth["statements"] if s["statement_id"] == "kr-checking_2025-06"))
    st["transactions"][0]["balance_after"] = str(Decimal(st["transactions"][0]["balance_after"]) + 1000)
    r = run(truth, [st])[normalize_account_number(st["fields"]["account_number"]["value"])]
    assert "arithmetic_mismatch" in {f.code for f in r.flags}


def test_adjacent_statements_that_dont_chain_are_flagged(truth):
    a, b = (copy.deepcopy(s) for s in truth["statements"][4:6])  # kr-checking May, June
    b["fields"]["opening_balance"]["value"] = str(Decimal(b["fields"]["opening_balance"]["value"]) + 5)
    r = run(truth, [a, b])[normalize_account_number(a["fields"]["account_number"]["value"])]
    assert "continuity_break" in {f.code for f in r.flags}


def test_unknown_ownership_and_currency_are_review_flags(truth):
    st = copy.deepcopy(truth["statements"][0])
    st["fields"]["currency"]["value"] = "XYZ"
    obs = [observe(reading_for(st), "u1")]
    (r,) = reconcile(obs, {}, rates.load(TAX_YEAR), TAX_YEAR)
    assert {"ownership_unknown", "currency_unknown"} <= {f.code for f in r.flags}
    assert r.max_value_usd is None


@pytest.mark.parametrize(
    "text, want",
    [
        ("2025.03.01 ~ 2025.03.31", ("2025-03-01", "2025-03-31")),
        ("01.03.2025 - 31.03.2025", ("2025-03-01", "2025-03-31")),
        ("01/05/2568 - 31/05/2568", ("2025-05-01", "2025-05-31")),
    ],
)
def test_parse_period(text, want):
    start, end = parse_period(text)
    assert (start.isoformat(), end.isoformat()) == want


@pytest.mark.parametrize(
    "text, want", [("KRW", "KRW"), ("EUR", "EUR"), ("₩", "KRW"), ("฿ (THB)", "THB"), ("", None)]
)
def test_parse_currency(text, want):
    assert parse_currency(text) == want
