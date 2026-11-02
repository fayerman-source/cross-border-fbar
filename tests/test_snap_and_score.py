"""Snapping and scoring, offline: readings are built from the answer key, then distorted."""

import pytest

from cross_border_fbar.eval.score import score_page, summarize
from cross_border_fbar.extract.snap import align_rows, snap_rows
from cross_border_fbar.synth.__main__ import FONTS, TAX_YEAR, rasterize
from cross_border_fbar.synth.household import household
from cross_border_fbar.synth.ledger import build_ledger, monthly_statements
from cross_border_fbar.synth.render import render_statement
from cross_border_fbar.synth.truth import major


def _box(b):
    return {"x_min": b["x0"], "y_min": b["y0"], "x_max": b["x1"], "y_max": b["y1"]}


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    """Korean checking, March (12 rows): its rendered page image and answer-key record."""
    out = tmp_path_factory.mktemp("page")
    spec = household(TAX_YEAR)[0]
    stmt = monthly_statements(spec, TAX_YEAR, build_ledger(spec, TAX_YEAR, 2025))[2]
    drawn = render_statement(spec, stmt, FONTS, out / "statements" / "p.pdf")
    (out / "pages").mkdir()
    image = out / rasterize(out / "statements" / "p.pdf", out / "pages", 150)[0]["file"]
    record = {
        "statement_id": stmt.statement_id,
        "layout": "kr",
        "period": "2025-03",
        "fields": {
            k: {**v, "value": major(v["value"], "KRW") if isinstance(v["value"], int) else v["value"]}
            for k, v in drawn["fields"].items()
        },
        "transactions": [
            {
                "txn_id": t.txn_id,
                "date": t.day.isoformat(),
                "amount": major(t.amount, "KRW"),
                "balance_after": major(t.balance_after, "KRW"),
                **drawn["lines"][t.txn_id],
            }
            for t in stmt.txns
        ],
    }
    return image, record


def perfect_reading(rec, drift=lambda y: y):
    """What a flawless model would return; `drift` distorts vertical positions."""

    def moved(b):
        bb = _box(b)
        return {**bb, "y_min": round(drift(bb["y_min"])), "y_max": round(drift(bb["y_max"]))}

    f = rec["fields"]
    fields = {
        n: {"text": str(f[n]["value"]), "status": "read", "bbox": _box(f[n]["text_bbox"])}
        for n in ("institution", "currency", "account_number", "holder", "opening_balance", "closing_balance")
    }
    fields["period"] = {"text": "", "status": "absent", "bbox": _box(f["period"]["bbox"])}
    rows = []
    for t in rec["transactions"]:
        debit = t["amount"].startswith("-")
        rows.append(
            {
                "date_text": t["date"].replace("-", "."),
                "description": "",
                "amount_text": t["amount"].lstrip("-"),
                "amount_column": "debit" if debit else "credit",
                "balance_text": t["balance_after"],
                "row_bbox": moved(t["row_bbox"]),
                "amount_bbox": moved(t["amount_text_bbox"]),
                "balance_bbox": moved(t["balance_text_bbox"]),
            }
        )
    return {**fields, "rows": rows}


def test_a_perfect_reading_scores_full_marks(page):
    image, rec = page
    s = summarize([score_page(rec, perfect_reading(rec), None)])["all"]
    n = len(rec["transactions"])
    assert s["fields_value_ok"] == "6/6"
    assert s["amount_ok"] == s["balance_ok"] == s["date_ok"] == s["row_centre_in_row"] == f"{n}/{n}"


def test_snapping_undoes_downward_compression(page):
    """Shrink positions towards the top of the table, as muse-spark-1.3 did in the step-1 probe."""
    image, rec = page
    top = rec["transactions"][0]["row_bbox"]["y0"]
    reading = perfect_reading(rec, drift=lambda y: top + 0.92 * (y - top))
    raw = score_page(rec, reading, None)
    snapped = snap_rows(reading, image)
    assert snapped is not None
    fixed = score_page(rec, reading, snapped)
    n = len(rec["transactions"])
    assert sum(r["row_centre_in_row"] for r in raw["rows"]) < n
    assert sum(r["snapped_row_centre_in_row"] for r in fixed["rows"]) == n


def test_alignment_abstains_when_two_runs_fit_equally():
    bands = [(0, 10), (20, 30), (40, 50), (60, 70)]
    # Model centres exactly between two candidate runs: no confident choice.
    assert align_rows([15.0, 35.0], bands) is None
    idx, margin = align_rows([5.0, 25.0], bands)
    assert idx == [0, 1]
    assert margin > 0


def test_eval_reports_statements_without_page_images(tmp_path):
    """A set generated without --png used to evaluate nothing and still pass."""
    import json as _json

    from cross_border_fbar.eval.__main__ import evaluate

    (tmp_path / "truth.json").write_text(
        _json.dumps({"generator": {}, "statements": [{"statement_id": "x_2025-01", "pages": 1}]}),
        encoding="utf-8",
    )
    result = evaluate(tmp_path, "muse-spark-1.3", "replay", workers=1, limit=None)
    assert result["summary"] == {}
    assert [e["statement_id"] for e in result["errors"]] == ["x_2025-01"]
    assert "--png" in result["errors"][0]["error"]


def test_shared_amount_column_gives_no_column_pattern():
    from PIL import Image

    from cross_border_fbar.extract.snap import _column_pattern

    box = {"x_min": 600, "y_min": 0, "x_max": 700, "y_max": 10}
    rows = [{"amount_bbox": box, "amount_column": "debit"}, {"amount_bbox": box, "amount_column": "credit"}]
    assert _column_pattern(rows, [(0, 10), (20, 30)], Image.new("L", (100, 100), 255)) is None


def test_fallback_alignment_accepts_strong_compression():
    """Run 3 on Oct 27: rows compressed 16%; a +/-15% scale guard excluded the right run."""
    bands = [(i * 22, i * 22 + 10) for i in range(20)]
    true_start = 11
    model = [bands[true_start][0] + 5 + i * 18.4 for i in range(6)]  # 16% tighter spacing
    idx, _ = align_rows(model, bands)
    assert idx[0] == true_start


def test_unprinted_fields_must_be_reported_absent_not_unreadable(page):
    """Empty-text 'unreadable' used to score as a correct absence."""
    from cross_border_fbar.eval.score import _score_fields

    _, rec = page
    view = {**rec, "fields": {k: v for k, v in rec["fields"].items() if k != "holder"}}
    reading = perfect_reading(rec)
    reading["holder"] = {"text": "", "status": "unreadable", "bbox": reading["holder"]["bbox"]}
    assert _score_fields(view, reading)["holder"]["value_ok"] is False
    reading["holder"]["status"] = "absent"
    assert _score_fields(view, reading)["holder"]["value_ok"] is True
