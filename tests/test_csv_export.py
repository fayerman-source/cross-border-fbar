"""Draft FinCEN Form 114 account schedule CSV export: classifications, unresolved flags, and HTTP routes."""

from __future__ import annotations

import csv
import http.client
import io
import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from PIL import Image

from cross_border_fbar.app import hosted
from cross_border_fbar.app.csv_export import COLUMNS, export_fincen114_csv
from cross_border_fbar.app.review import OUT_OF_SCOPE, Review, ReviewError
from cross_border_fbar.app.server import make_handler
from cross_border_fbar.app.worksheet import Inputs
from cross_border_fbar.extract.page import FIELD_NAMES

BOX = {"x_min": 10, "y_min": 10, "x_max": 200, "y_max": 30}


def _page_reading(
    account_number: str,
    institution: str,
    currency: str,
    period: str,
    opening: str | None,
    closing: str | None,
    rows: list[dict],
) -> dict:
    fields = {name: {"text": "", "status": "absent", "bbox": BOX} for name in FIELD_NAMES}
    if institution:
        fields["institution"] = {"text": institution, "status": "read", "bbox": BOX}
    if currency:
        fields["currency"] = {"text": currency, "status": "read", "bbox": BOX}
    if account_number:
        fields["account_number"] = {"text": account_number, "status": "read", "bbox": BOX}
    fields["holder"] = {"text": "JUNG SEULGI JOY", "status": "read", "bbox": BOX}
    fields["period"] = {"text": period, "status": "read", "bbox": BOX}
    if opening is not None:
        fields["opening_balance"] = {"text": opening, "status": "read", "bbox": BOX}
    if closing is not None:
        fields["closing_balance"] = {"text": closing, "status": "read", "bbox": BOX}
    return {**fields, "rows": rows}


@pytest.fixture
def mini_inputs(tmp_path) -> Inputs:
    set_dir = tmp_path / "set"
    pages_dir = set_dir / "pages"
    pages_dir.mkdir(parents=True)
    for stem in ("sole", "joint", "sig", "nobal", "masked"):
        Image.new("RGB", (100, 100), "white").save(pages_dir / f"{stem}-p1.png")

    readings = {
        "model": "muse-spark-1.3",
        "prompt": "statement_page_v3",
        "pages": [
            {
                "statement_id": "sole",
                "page": 1,
                "reading": _page_reading(
                    "110-111",
                    "=Saebyeol Bank",
                    "KRW",
                    "2025.01.01 ~ 2025.12.31",
                    "10,000,000",
                    "20,000,000",
                    [
                        {
                            "date_text": "2025.03.10",
                            "account_text": "",
                            "description": "dep",
                            "amount_text": "10,000,000",
                            "amount_column": "credit",
                            "balance_text": "20,000,000",
                            "row_bbox": BOX,
                            "amount_bbox": BOX,
                            "balance_bbox": BOX,
                        }
                    ],
                ),
            },
            {
                "statement_id": "joint",
                "page": 1,
                "reading": _page_reading(
                    "DE360001",
                    "Bergwald Bank AG",
                    "EUR",
                    "01.01.2025 - 31.12.2025",
                    "5.000,00",
                    "5.000,00",
                    [],
                ),
            },
            {
                "statement_id": "sig",
                "page": 1,
                "reading": _page_reading(
                    "302-222",
                    "Daon Bank",
                    "KRW",
                    "2025.01.01 ~ 2025.11.30",
                    "30,000,000",
                    "30,000,000",
                    [],
                ),
            },
            {
                "statement_id": "nobal",
                "page": 1,
                "reading": _page_reading(
                    "110-333",
                    "Saebyeol Bank",
                    "KRW",
                    "2025.01.01 ~ 2025.12.31",
                    None,
                    None,
                    [
                        {
                            "date_text": "2025.06.01",
                            "account_text": "",
                            "description": "tx",
                            "amount_text": "1,000,000",
                            "amount_column": "credit",
                            "balance_text": "",
                            "row_bbox": BOX,
                            "amount_bbox": BOX,
                            "balance_bbox": BOX,
                        }
                    ],
                ),
            },
            {
                "statement_id": "masked",
                "page": 1,
                "reading": _page_reading(
                    "110-***-****",
                    "Saebyeol Bank",
                    "KRW",
                    "2025.01.01 ~ 2025.12.31",
                    None,
                    None,
                    [
                        {
                            "date_text": "2025.04.01",
                            "account_text": "",
                            "description": "m",
                            "amount_text": "500,000",
                            "amount_column": "credit",
                            "balance_text": "",
                            "row_bbox": BOX,
                            "amount_bbox": BOX,
                            "balance_bbox": BOX,
                        }
                    ],
                ),
            },
        ],
    }
    readings_path = tmp_path / "readings.json"
    readings_path.write_text(json.dumps(readings), encoding="utf-8")

    intake = {
        "client": "+Joy Seulgi Jung",
        "source": "synthetic test intake",
        "household": {"filing_status": "mfs", "lives_abroad": True, "spouse_specified_individual": False},
        "accounts": [
            {"account_number": "110-111", "ownership": "sole", "financial_interest": True},
            {"account_number": "DE360001", "ownership": "joint", "financial_interest": True},
            {"account_number": "302-222", "ownership": "signature_authority", "financial_interest": False},
            {"account_number": "110-333", "ownership": "sole", "financial_interest": True},
        ],
    }
    intake_path = set_dir / "intake.json"
    intake_path.write_text(json.dumps(intake), encoding="utf-8")
    return Inputs(set_dir, readings_path, intake_path)


def test_csv_export_requires_signoff_maps_parts_preserves_flags_and_escapes_formulas(mini_inputs, tmp_path):
    review = Review(mini_inputs, tmp_path / "rv", root=tmp_path)
    with pytest.raises(ReviewError, match="Sign off"):
        review.export_csv()

    for fid in review.open_review_flags():
        review.acknowledge(fid, "+checked synthetic source", "A. Rivera")
    review.sign_off("A. Rivera", "AR")

    csv_text = review.export_csv()
    assert not OUT_OF_SCOPE.search(csv_text)
    assert "filing-ready" not in csv_text.lower()
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert tuple(rows[0].keys()) == COLUMNS

    by_acct = {r["account_number"]: r for r in rows if r["row_kind"] != "worksheet_summary"}
    assert by_acct["110-111"]["fincen_114_part"] == "Part II (financial interest, separate)"
    assert by_acct["110-111"]["institutions"] == "'=Saebyeol Bank"  # formula prefix neutralized
    assert by_acct["110-111"]["client"] == "'+Joy Seulgi Jung"  # leading '+' neutralized
    assert by_acct["DE360001"]["fincen_114_part"] == "Part III (financial interest, joint)"
    assert by_acct["302-222"]["fincen_114_part"] == "Part IV (signature authority, no financial interest)"
    assert "missing_statement" in by_acct["302-222"]["unresolved_review_flags"]
    assert "signature_authority_only" in by_acct["302-222"]["unresolved_review_flags"]
    assert "+checked synthetic source (A. Rivera)" in by_acct["302-222"]["preparer_acknowledgements"]
    assert by_acct["110-333"]["max_value_usd"] == "UNDETERMINED"
    assert "no_balance_anchor" in by_acct["110-333"]["unresolved_review_flags"]
    assert by_acct["110-***-****"]["row_kind"] == "excluded_unidentified"
    assert "account_number_masked" in by_acct["110-***-****"]["unresolved_review_flags"]

    summary = next(r for r in rows if r["row_kind"] == "worksheet_summary")
    assert int(summary["max_value_usd"]) == review.worksheet["totals"]["aggregate_usd"]
    assert summary["open_determinations"]
    assert summary["signoff_fingerprint"] == review.state["signoff"]["hash"]


def test_local_and_hosted_servers_serve_csv_export_only_after_signoff(mini_inputs, tmp_path):
    # Local server
    rv = Review(mini_inputs, tmp_path / "local_rv", root=tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rv, mini_inputs.set_dir))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/export.csv", headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        res.read()
        assert res.status == 409
        conn.close()

        for fid in rv.open_review_flags():
            rv.acknowledge(fid, "checked", "Reviewer")
        rv.sign_off("Reviewer", "RV")

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/export.csv", headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        assert res.status == 200
        assert res.getheader("Content-Type") == "text/csv; charset=utf-8"
        assert "Part II (financial interest, separate)" in body
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()

    # Hosted server per-visitor isolation
    store = hosted.VisitorStore(mini_inputs)
    hsrv = hosted.HostedServer(("127.0.0.1", 0), hosted.make_hosted_handler(store, "demo.example.org"), 8)
    threading.Thread(target=hsrv.serve_forever, daemon=True).start()
    hport = hsrv.server_address[1]
    try:
        # Visitor B has not signed off -> 409
        conn = http.client.HTTPConnection("127.0.0.1", hport, timeout=10)
        conn.request("GET", "/api/export.csv", headers={"Host": "demo.example.org"})
        res = conn.getresponse()
        res.read()
        assert res.status == 409
        conn.close()
    finally:
        hsrv.shutdown()
        hsrv.server_close()
        store.close()


def test_unresolvable_classifications_never_guess_part_numbers():
    fake = {
        "worksheet": {
            "tax_year": 2025,
            "client": "Synthetic",
            "accounts": [
                {
                    "key": "A",
                    "account_number": "A",
                    "identified": True,
                    "institutions": ["Bank"],
                    "holders": ["Holder"],
                    "ownership": "sole",
                    "financial_interest": False,
                    "currency": "EUR",
                    "max": None,
                    "rate": "0.851",
                    "rate_basis": "treasury",
                    "rate_date": "2025-12-31",
                    "max_usd": None,
                    "uploads": [],
                    "flags": [],
                },
                {
                    "key": "B",
                    "account_number": "B",
                    "identified": True,
                    "institutions": ["Bank"],
                    "holders": ["Holder"],
                    "ownership": "signature_authority",
                    "financial_interest": None,
                    "currency": "EUR",
                    "max": None,
                    "rate": "0.851",
                    "rate_basis": "treasury",
                    "rate_date": "2025-12-31",
                    "max_usd": None,
                    "uploads": [],
                    "flags": [],
                },
            ],
            "totals": {"aggregate_usd": 0, "threshold_usd": 10000, "above_threshold": None},
        },
        "signoff": {"hash": "abc123"},
        "remaining_review_items": [],
        "open_determinations": [],
    }
    rows = list(csv.DictReader(io.StringIO(export_fincen114_csv(fake))))
    assert rows[0]["fincen_114_part"].startswith("Unresolved")
    assert rows[1]["fincen_114_part"].startswith("Unresolved")
