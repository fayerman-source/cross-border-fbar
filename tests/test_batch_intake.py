"""Batch multi-page PDF/CSV ingestion and UI intake editor tests."""

from __future__ import annotations

import base64
import http.client
import json
import threading
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from cross_border_fbar.app import hosted
from cross_border_fbar.app.review import Review, ReviewError
from cross_border_fbar.app.server import make_handler
from cross_border_fbar.app.worksheet import Inputs, build
from cross_border_fbar.eval import photo
from cross_border_fbar.synth.household import SAEBYEOL
from cross_border_fbar.synth.model import AccountSpec, BankSpan, Statement, Txn
from cross_border_fbar.synth.render import render_compiled, render_statement

FONTS = Path(__file__).resolve().parents[1] / "src" / "cross_border_fbar" / "fonts"


def _make_multipage_compiled_pdf(out: Path) -> None:
    rows = [
        SimpleNamespace(
            row_id=f"r{i}",
            day=date(2025, 1, (i % 28) + 1),
            account_label="MMDA",
            account_printed="110-731-552904",
            channel="인터넷",
            direction="입금",
            amount=1_000_000,
            counterparty="정슬기",
            remark="본인",
        )
        for i in range(42)
    ]
    rendered = render_compiled(rows, (date(2025, 1, 1), date(2025, 1, 31)), FONTS, out)
    assert rendered["pages"] == 2


def _make_single_page_pdf(out: Path) -> None:
    spec = AccountSpec(
        account_id="kr-sole",
        currency="KRW",
        account_number="110-482-913746",
        product="Checking",
        holder_printed="JUNG SEULGI JOY",
        ownership="sole",
        financial_interest=True,
        owners=("JUNG SEULGI JOY",),
        opening_balance=0,
        banks=(BankSpan(SAEBYEOL, 1, 12),),
        monthly=lambda rng, y, m: [],
    )
    stmt = Statement(
        account_id="kr-sole",
        year=2025,
        month=3,
        bank=SAEBYEOL,
        opening_balance=20_000_000,
        closing_balance=30_000_000,
        txns=(Txn("t1", date(2025, 3, 10), "Deposit", 10_000_000, 30_000_000, "routine"),),
    )
    render_statement(spec, stmt, FONTS, out)


def test_run_batch_ingests_directory_with_multipage_pdf_and_csv_at_zero_cost(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _make_multipage_compiled_pdf(inbox / "01_compiled.pdf")
    _make_single_page_pdf(inbox / "02_kr_2025-03.pdf")
    (inbox / "03_eur.csv").write_text(
        "Institution,Bergwald Bank AG\n"
        "Account Number,DE36990123450481527360\n"
        "Currency,EUR\n"
        "Period,2025-01-01 ~ 2025-12-31\n"
        "Opening Balance,12000.00\n"
        "Closing Balance,12500.00\n"
        "Date,Description,Amount,Balance\n"
        "2025-05-01,Interest,500.00,12500.00\n",
        encoding="utf-8",
    )

    set_dir = tmp_path / "review_set"
    res = photo.run_batch([inbox], set_dir, synthetic=True)
    assert res["files_ingested"] == 3
    assert res["pages_ingested"] == 4  # 2 pages from compiled.pdf + 1 page kr + 1 CSV
    assert res["cost_usd"] == 0.0

    doc = json.loads((set_dir / "readings.json").read_text(encoding="utf-8"))
    assert len(doc["pages"]) == 4
    # Multi-page PDF keeps pages 1 and 2 grouped under the same statement_id
    by_stmt: dict[str, list[int]] = {}
    for p in doc["pages"]:
        by_stmt.setdefault(p["statement_id"], []).append(p["page"])
    assert sorted(by_stmt.values()) == [[1], [1], [1, 2]]

    ws = build(Inputs(set_dir, set_dir / "readings.json", set_dir / "intake.json"))
    by_acct = {a["key"]: a for a in ws["accounts"]}
    assert by_acct["110482913746"]["max"]["amount"] == "30000000"
    assert by_acct["DE36990123450481527360"]["max"]["amount"] == "12500.00"


def test_intake_editor_updates_household_and_accounts_and_upload_endpoint_is_local_only(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    seed_pdf = tmp_path / "seed.pdf"
    _make_single_page_pdf(seed_pdf)
    set_dir = tmp_path / "set"
    photo.run_batch([seed_pdf], set_dir, synthetic=True)
    # Seed extra document, household, and account metadata to verify form saves preserve unedited metadata.
    (set_dir / "intake.json").write_text(
        json.dumps(
            {
                "client": "Initial",
                "source": "Initial",
                "engagement_code": "ENG-2025",
                "household": {"residency_country": "KR"},
                "accounts": [
                    {
                        "account_number": "110-482-913746",
                        "ownership": "unknown",
                        "preparer_tag": "primary-checking",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    inp = Inputs(set_dir, set_dir / "readings.json", set_dir / "intake.json")

    rv = Review(inp, tmp_path / "rv", root=tmp_path)
    assert rv.worksheet["accounts"][0]["ownership"] == "unknown"
    assert rv.worksheet["form8938"]["year_end"]["threshold_usd"] is None

    with pytest.raises(ReviewError, match="note"):
        rv.update_intake({"accounts": []}, "", "A. Rivera")

    rv.update_intake(
        {
            "client": "Joy Seulgi Jung",
            "source": "Engagement organizer",
            "household": {
                "filing_status": "single",
                "lives_abroad": True,
                "spouse_specified_individual": None,
            },
            "accounts": [
                {
                    "account_number": "110-482-913746",
                    "ownership": "sole",
                    "financial_interest": True,
                    "joint_with_spouse": False,
                }
            ],
        },
        "Entered from signed organizer",
        "A. Rivera",
    )
    assert rv.worksheet["client"] == "Joy Seulgi Jung"
    assert rv.worksheet["accounts"][0]["ownership"] == "sole"
    assert rv.worksheet["form8938"]["year_end"]["threshold_usd"] == "200000"
    assert rv.worksheet["intake"]["engagement_code"] == "ENG-2025"
    assert rv.worksheet["intake"]["household"]["residency_country"] == "KR"
    assert rv.worksheet["intake"]["accounts"][0]["preparer_tag"] == "primary-checking"

    # Exercise local HTTP /api/upload (ingesting a 2-page PDF) and /api/intake
    multipage_pdf = tmp_path / "multi.pdf"
    _make_multipage_compiled_pdf(multipage_pdf)
    b64_pdf = base64.b64encode(multipage_pdf.read_bytes()).decode("ascii")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rv, set_dir))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        headers = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
        # Unconfirmed synthetic flag -> 409
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/upload",
            body=json.dumps(
                {"synthetic_confirmed": False, "files": [{"name": "m.pdf", "data_b64": b64_pdf}]}
            ),
            headers=headers,
        )
        res = conn.getresponse()
        res.read()
        assert res.status == 409
        conn.close()

        # Confirmed synthetic upload -> 200 and serves newly rendered page-2 PNG
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/upload",
            body=json.dumps({"synthetic_confirmed": True, "files": [{"name": "m.pdf", "data_b64": b64_pdf}]}),
            headers=headers,
        )
        res = conn.getresponse()
        state_data = json.loads(res.read().decode("utf-8"))
        assert res.status == 200
        conn.close()

        p2_uploads = [u for u in state_data["page_readings"] if u.endswith("-p2.png")]
        assert len(p2_uploads) == 1
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/doc/{p2_uploads[0]}", headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        res.read()
        assert res.status == 200
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()

    # Hosted server refuses /api/upload (404 on small payload, 413 on oversized payload)
    store = hosted.VisitorStore(inp)
    hsrv = hosted.HostedServer(("127.0.0.1", 0), hosted.make_hosted_handler(store, "demo.example.org"), 8)
    threading.Thread(target=hsrv.serve_forever, daemon=True).start()
    hport = hsrv.server_address[1]
    try:
        h_headers = {
            "Host": "demo.example.org",
            "Origin": "https://demo.example.org",
            "Content-Type": "application/json",
        }
        conn = http.client.HTTPConnection("127.0.0.1", hport, timeout=10)
        conn.request(
            "POST",
            "/api/upload",
            body=json.dumps({"synthetic_confirmed": True, "files": [{"name": "a.csv", "data_b64": "YQ=="}]}),
            headers=h_headers,
        )
        res = conn.getresponse()
        res.read()
        assert res.status == 404
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", hport, timeout=10)
        conn.request(
            "POST",
            "/api/upload",
            body=json.dumps({"synthetic_confirmed": True, "files": [{"name": "m.pdf", "data_b64": b64_pdf}]}),
            headers=h_headers,
        )
        res = conn.getresponse()
        res.read()
        assert res.status == 413
        conn.close()
    finally:
        hsrv.shutdown()
        hsrv.server_close()
        store.close()
