import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from cross_border_fbar import rates
from cross_border_fbar.synth.__main__ import FONTS, TAX_YEAR, generate
from cross_border_fbar.synth.household import household
from cross_border_fbar.synth.ledger import build_ledger, monthly_statements
from cross_border_fbar.synth.render import render_statement
from cross_border_fbar.synth.truth import to_usd_ceiling

SEED = 2025


@pytest.fixture(scope="module")
def truth(tmp_path_factory) -> dict:
    return generate(tmp_path_factory.mktemp("synth"), SEED, png=False, dpi=150, threshold_usd=1000)


def by_id(truth, account_id):
    return next(a for a in truth["accounts"] if a["account_id"] == account_id)


def test_ledger_is_deterministic_per_seed():
    specs = household(TAX_YEAR)
    assert all(build_ledger(s, TAX_YEAR, SEED) == build_ledger(s, TAX_YEAR, SEED) for s in specs)
    assert any(build_ledger(s, TAX_YEAR, SEED) != build_ledger(s, TAX_YEAR, SEED + 1) for s in specs)


def test_statements_chain_and_balance():
    for spec in household(TAX_YEAR):
        stmts = monthly_statements(spec, TAX_YEAR, build_ledger(spec, TAX_YEAR, SEED))
        assert stmts[0].opening_balance == spec.opening_balance
        for prev, cur in zip(stmts, stmts[1:], strict=False):
            assert cur.opening_balance == prev.closing_balance
        for s in stmts:
            assert s.opening_balance + sum(t.amount for t in s.txns) == s.closing_balance
            assert all(t.balance_after >= 0 for t in s.txns)


def test_pdf_bytes_are_reproducible(tmp_path):
    spec = household(TAX_YEAR)[3]
    stmt = monthly_statements(spec, TAX_YEAR, build_ledger(spec, TAX_YEAR, SEED))[4]
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    render_statement(spec, stmt, FONTS, a)
    render_statement(spec, stmt, FONTS, b)
    assert hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()


def test_withheld_month_is_absent_and_flagged(truth):
    ids = {s["statement_id"] for s in truth["statements"]}
    assert "kr-parents_2025-08" not in ids
    parents = by_id(truth, "kr-parents")
    assert parents["statements_withheld"] == ["kr-parents_2025-08"]
    assert {"missing_statement", "signature_authority_only"} <= {f["code"] for f in parents["expected_flags"]}
    # The true August peak is recorded but is not the reportable maximum.
    assert Decimal(parents["hidden_true_peak"]["amount"]) > Decimal(parents["max_value"]["amount"])


def test_mid_month_peak_is_the_maximum_not_a_month_end(truth):
    kr = by_id(truth, "kr-checking")
    assert kr["max_value"]["statement_id"] == "kr-checking_2025-03"
    assert kr["max_value"]["source"] not in ("opening_balance", "closing_balance")
    assert Decimal(kr["max_value"]["amount"]) > Decimal(kr["max_month_end"]["amount"])


def test_intraday_flag_respects_threshold(truth):
    th = by_id(truth, "th-savings")
    flagged = [c for c in th["intraday_candidates"] if c["flag"]]
    assert [c["date"] for c in flagged] == ["2025-05-16"]
    kr = by_id(truth, "kr-checking")
    # A small debit-before-credit day exists but stays under the $1,000 floor.
    assert kr["intraday_candidates"] and not any(c["flag"] for c in kr["intraday_candidates"])


def test_usd_conversion_rounds_up_and_floors_negative_at_zero():
    krw = rates.load(TAX_YEAR)["KRW"]
    assert to_usd_ceiling(1_443_750, "KRW", krw) == 1_000  # exact
    assert to_usd_ceiling(1_443_751, "KRW", krw) == 1_001  # any fraction rounds up
    assert to_usd_ceiling(-5_000, "KRW", krw) == 0


def test_boxes_are_on_the_grid_and_rows_do_not_overlap(truth):
    for s in truth["statements"]:
        rows = []
        for t in s["transactions"]:
            b = t["row_bbox"]
            assert 0 <= b["x0"] < b["x1"] <= 1000 and 0 <= b["y0"] < b["y1"] <= 1000
            rows.append((t["page"], b["y0"], b["y1"]))
            assert (t["balance_bbox"] is not None) == s["prints_running_balance"]
        for (p1, _, end), (p2, start, _) in zip(rows, rows[1:], strict=False):
            assert p1 < p2 or end <= start


def test_truth_is_json_serializable_and_marked_synthetic(truth):
    assert truth["synthetic"] is True
    assert truth["exchange_rates"]["source"]["api_query"].startswith(rates.API_URL)
    json.dumps(truth, ensure_ascii=False)
    assert Path(FONTS / "OFL.txt").exists()


def test_text_boxes_sit_inside_their_cells(truth):
    def inside(inner, outer):
        return (
            outer["x0"] <= inner["x0"] < inner["x1"] <= outer["x1"]
            and outer["y0"] <= inner["y0"] < inner["y1"] <= outer["y1"]
        )

    for s in truth["statements"]:
        for f in s["fields"].values():
            assert inside(f["text_bbox"], f["bbox"])
        for t in s["transactions"]:
            assert inside(t["amount_text_bbox"], t["amount_bbox"])
            if t["balance_bbox"]:
                assert inside(t["balance_text_bbox"], t["balance_bbox"])
