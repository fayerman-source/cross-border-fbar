"""Draft PDF review workpaper export: sign-off gate, embedded source crops, flags, and HTTP routes."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from cross_border_fbar.app import hosted
from cross_border_fbar.app.review import OUT_OF_SCOPE, Review, ReviewError
from cross_border_fbar.app.server import make_handler
from cross_border_fbar.app.worksheet import Inputs
from cross_border_fbar.extract.page import FIELD_NAMES

BOX = {"x_min": 40, "y_min": 180, "x_max": 920, "y_max": 220}


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
    fields["institution"] = {"text": institution, "status": "read", "bbox": BOX}
    fields["currency"] = {"text": currency, "status": "read", "bbox": BOX}
    fields["account_number"] = {"text": account_number, "status": "read", "bbox": BOX}
    fields["holder"] = {"text": "JUNG SEULGI JOY", "status": "read", "bbox": BOX}
    fields["period"] = {"text": period, "status": "read", "bbox": BOX}
    if opening is not None:
        fields["opening_balance"] = {"text": opening, "status": "read", "bbox": BOX}
    if closing is not None:
        fields["closing_balance"] = {"text": closing, "status": "read", "bbox": BOX}
    return {**fields, "rows": rows}


@pytest.fixture
def workpaper_inputs(tmp_path) -> Inputs:
    set_dir = tmp_path / "set"
    pages_dir = set_dir / "pages"
    pages_dir.mkdir(parents=True)
    for stem in ("sole", "sig"):
        img = Image.new("RGB", (400, 300), "white")
        draw = ImageDraw.Draw(img)
        draw.rectangle((16, 54, 368, 66), fill=(30, 30, 30))
        img.save(pages_dir / f"{stem}-p1.png")

    readings = {
        "model": "muse-spark-1.3",
        "prompt": "statement_page_v3",
        "pages": [
            {
                "statement_id": "sole",
                "page": 1,
                "reading": _page_reading(
                    "110-111",
                    "Saebyeol Bank",
                    "KRW",
                    "2025.01.01 ~ 2025.12.31",
                    "10,000,000",
                    "20,000,000",
                    [
                        {
                            "date_text": "2025.03.10",
                            "account_text": "",
                            "description": "synthetic deposit",
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
        ],
    }
    readings_path = tmp_path / "readings.json"
    readings_path.write_text(json.dumps(readings), encoding="utf-8")
    intake_path = set_dir / "intake.json"
    intake_path.write_text(
        json.dumps(
            {
                "client": "Joy Seulgi Jung",
                "source": "synthetic test intake",
                "accounts": [
                    {"account_number": "110-111", "ownership": "sole", "financial_interest": True},
                    {
                        "account_number": "302-222",
                        "ownership": "signature_authority",
                        "financial_interest": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return Inputs(set_dir, readings_path, intake_path)


def _pdf_text(pdf_bytes: bytes) -> str:
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        chunks = []
        for idx in range(len(doc)):
            page = doc[idx]
            tp = page.get_textpage()
            chunks.append(tp.get_text_range())
            tp.close()
            page.close()
        return "\n".join(chunks)
    finally:
        doc.close()


def test_workpaper_pdf_requires_signoff_embeds_crops_flags_and_draft_banner(workpaper_inputs, tmp_path):
    review = Review(workpaper_inputs, tmp_path / "rv", root=tmp_path)
    with pytest.raises(ReviewError, match="Sign off"):
        review.export_pdf()

    for fid in review.open_review_flags():
        review.acknowledge(fid, "checked synthetic statement", "A. Rivera")
    review.sign_off("A. Rivera", "AR")

    pdf_bytes = review.export_pdf()
    assert pdf_bytes.startswith(b"%PDF-")
    assert b"/Subtype /Image" in pdf_bytes  # cropped source-line PNG strip embedded

    extracted = _pdf_text(pdf_bytes)
    assert "Draft schedule for preparer review; not a filing determination" in extracted
    assert "filing-ready" not in extracted.lower()
    assert "CPA" not in extracted
    assert not OUT_OF_SCOPE.search(extracted)
    assert "Part II (financial interest, separate)" in extracted
    assert "Part IV (signature authority, no financial interest)" in extracted
    assert "missing_statement" in extracted
    assert "checked synthetic statement" in extracted
    assert review.state["signoff"]["hash"] in extracted


def test_workpaper_pdf_preserves_reviewer_value_correction_provenance(workpaper_inputs, tmp_path):
    review = Review(workpaper_inputs, tmp_path / "rv_corr", root=tmp_path)
    review.correct_value(
        "pages/sole-p1.png",
        "row:0:amount",
        "15,000,000",
        "verified credit slip on page 1",
        "A. Rivera",
    )
    review.correct_value(
        "pages/sole-p1.png",
        "closing_balance",
        "25,000,000",
        "verified closing stamp on page 1",
        "A. Rivera",
    )
    for fid in review.open_review_flags():
        review.acknowledge(fid, "checked synthetic statement", "A. Rivera")
    # Pad audit log past 40 entries to prove Schedule A retains correction provenance independently.
    for idx in range(42):
        review._audit("note_check", "A. Rivera", field=f"pad_{idx}")
    review.sign_off("A. Rivera", "AR")

    extracted = _pdf_text(review.export_pdf())
    flat = " ".join(extracted.split())
    assert "reviewer-corrected" in flat
    assert "read 10,000,000 -> corrected to 15000000 (15,000,000) by A. Rivera" in flat
    assert "verified credit slip on page 1" in flat
    assert "read 20,000,000 -> corrected to 25000000 (25,000,000) by A. Rivera" in flat
    assert "verified closing stamp on page 1" in flat
    assert "\u2014" not in extracted
    assert "CPA" not in extracted
    assert not OUT_OF_SCOPE.search(extracted)


def test_local_and_hosted_servers_serve_pdf_workpaper_only_after_signoff(workpaper_inputs, tmp_path):
    rv = Review(workpaper_inputs, tmp_path / "local_rv", root=tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rv, workpaper_inputs.set_dir))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/export.pdf", headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        res.read()
        assert res.status == 409
        conn.close()

        for fid in rv.open_review_flags():
            rv.acknowledge(fid, "checked", "Reviewer")
        rv.sign_off("Reviewer", "RV")

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/export.pdf", headers={"Host": f"127.0.0.1:{port}"})
        res = conn.getresponse()
        body = res.read()
        assert res.status == 200
        assert res.getheader("Content-Type") == "application/pdf"
        assert body.startswith(b"%PDF-")
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()

    store = hosted.VisitorStore(workpaper_inputs)
    hsrv = hosted.HostedServer(("127.0.0.1", 0), hosted.make_hosted_handler(store, "demo.example.org"), 8)
    threading.Thread(target=hsrv.serve_forever, daemon=True).start()
    hport = hsrv.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", hport, timeout=10)
        conn.request("GET", "/api/export.pdf", headers={"Host": "demo.example.org"})
        res = conn.getresponse()
        res.read()
        assert res.status == 409
        conn.close()
    finally:
        hsrv.shutdown()
        hsrv.server_close()
        store.close()
