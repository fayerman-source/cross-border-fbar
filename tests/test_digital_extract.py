"""Tests for deterministic born-digital PDF and CSV statement extraction and vision fallback."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fpdf import FPDF
from PIL import Image

from cross_border_fbar.app.worksheet import Inputs, build
from cross_border_fbar.eval import photo
from cross_border_fbar.eval.score import score_page
from cross_border_fbar.extract.digital import read_csv_statement, read_digital_pdf_page
from cross_border_fbar.extract.page import FIELD_NAMES, SCHEMA, read_statement_source
from cross_border_fbar.llm import Client
from cross_border_fbar.synth.household import BERGWALD, RHEINAUEN, SAEBYEOL, SIAM_LOTUS
from cross_border_fbar.synth.model import AccountSpec, BankSpan, Statement, Txn
from cross_border_fbar.synth.render import render_compiled, render_statement

FONTS = Path(__file__).resolve().parents[1] / "src" / "cross_border_fbar" / "fonts"
BANKS = {
    "saebyeol": SAEBYEOL,
    "rheinauen": RHEINAUEN,
    "bergwald": BERGWALD,
    "siam": SIAM_LOTUS,
}


def _spec(key: str, currency: str, account_number: str) -> AccountSpec:
    bank = BANKS[key]
    return AccountSpec(
        account_id=f"syn-{key}",
        currency=currency,
        account_number=account_number,
        product="Synthetic Account",
        holder_printed="JUNG SEULGI JOY",
        ownership="sole",
        financial_interest=True,
        owners=("JUNG SEULGI JOY",),
        opening_balance=0,
        banks=(BankSpan(bank, 1, 12),),
        monthly=lambda rng, y, m: [],
    )


@pytest.mark.parametrize(
    ("bank_key", "currency", "acct", "open_bal", "amount", "close_bal"),
    [
        ("saebyeol", "KRW", "110-482-913746", 10_000_000, 5_000_000, 15_000_000),
        ("rheinauen", "EUR", "DE36 9901 2345 0481 5273 60", 184_000, -31_000, 153_000),
        ("bergwald", "EUR", "DE36 9901 2345 0481 5273 60", 216_655, 95_000, 311_655),
        ("siam", "THB", "714-2-38065-9", 41_235_025, -20_000, 41_215_025),
    ],
)
def test_born_digital_pdfs_parse_across_bank_layouts_with_zero_api_calls(
    tmp_path, bank_key, currency, acct, open_bal, amount, close_bal
):
    spec = _spec(bank_key, currency, acct)
    stmt = Statement(
        account_id=spec.account_id,
        year=2025,
        month=3,
        bank=BANKS[bank_key],
        opening_balance=open_bal,
        closing_balance=close_bal,
        txns=(
            Txn(
                txn_id="t1",
                day=date(2025, 3, 10),
                description="Synthetic transfer",
                amount=amount,
                balance_after=close_bal,
                tag="routine",
            ),
        ),
    )
    pdf_path = tmp_path / f"{bank_key}_2025-03.pdf"
    rendered = render_statement(spec, stmt, FONTS, pdf_path)

    reading, meta = read_statement_source(pdf_path, page=1)
    assert meta["model"] == "digital-pdf"
    assert meta["cost_usd"] == 0.0
    photo._validate_schema(reading, SCHEMA)

    def fmt_val(minor: int) -> str:
        return f"{minor / 100:.2f}" if currency in ("EUR", "THB") else str(minor)

    truth_view = {
        "statement_id": f"{bank_key}_2025-03",
        "layout": BANKS[bank_key].layout,
        "period": "2025-03",
        "fields": {
            k: {
                **v,
                "value": fmt_val(open_bal)
                if k == "opening_balance"
                else (fmt_val(close_bal) if k == "closing_balance" else v["value"]),
            }
            for k, v in rendered["fields"].items()
            if v["page"] == 1
        },
        "transactions": [
            {
                "page": 1,
                "txn_id": "t1",
                "date": "2025-03-10",
                "amount": fmt_val(amount),
                "balance_after": fmt_val(close_bal),
                **rendered["lines"]["t1"],
            }
        ],
        "doc_currency": currency,
    }
    scored = score_page(truth_view, reading, reading)
    assert all(v["value_ok"] for v in scored["fields"].values())
    assert scored["rows_returned"] == 1
    assert scored["rows"][0]["date_ok"] and scored["rows"][0]["amount_ok"] and scored["rows"][0]["balance_ok"]


def test_compiled_multi_account_ledger_pdf_parses_both_pages_locally(tmp_path):
    rows = [
        SimpleNamespace(
            row_id=f"r{i}",
            day=date(2025, 1, (i % 28) + 1),
            account_label="MMDA",
            account_printed="110-731-552904" if i % 5 else "110-48*-******",
            channel="인터넷",
            direction="입금" if i % 2 == 0 else "출금",
            amount=1_000_000 + i * 10_000,
            counterparty="정슬기",
            remark="본인 계좌 간 이체",
        )
        for i in range(42)
    ]
    pdf_path = tmp_path / "compiled.pdf"
    rendered = render_compiled(rows, (date(2025, 1, 1), date(2025, 1, 31)), FONTS, pdf_path)
    assert rendered["pages"] == 2

    p1 = read_digital_pdf_page(pdf_path, page=1)
    p2 = read_digital_pdf_page(pdf_path, page=2)
    assert p1 is not None and p2 is not None
    assert len(p1["rows"]) + len(p2["rows"]) == 42
    assert p2["currency"]["text"] == "원"
    assert any(r["account_text"] == "110-48*-******" for r in p1["rows"])


def test_raster_or_inconsistent_pdf_falls_back_to_vision_without_guessing(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    # 1. Raster (image-only) PDF has no text layer -> returns None and triggers vision fallback.
    png_path = tmp_path / "scan.png"
    Image.new("RGB", (200, 280), "white").save(png_path)
    raster_pdf = tmp_path / "scanned.pdf"
    pdf = FPDF(unit="mm", format="A4")
    pdf.add_page()
    pdf.image(str(png_path), x=10, y=10, w=180)
    pdf.output(str(raster_pdf))
    assert read_digital_pdf_page(raster_pdf, page=1) is None

    sent = []
    zero = {"x_min": 0, "y_min": 0, "x_max": 0, "y_max": 0}
    stub_reading = {name: {"text": "", "status": "absent", "bbox": zero} for name in FIELD_NAMES}
    stub_reading["rows"] = []
    client = Client(mode="live", cap_usd=2, ledger=tmp_path / "ledger.jsonl", recordings=tmp_path / "rec")
    monkeypatch.setattr(
        client,
        "_post",
        lambda body: (
            sent.append(body["model"])
            or {
                "choices": [{"message": {"content": json.dumps(stub_reading)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ),
    )
    photo.run(raster_pdf, tmp_path / "scan_set", synthetic=True, client=client)
    assert sent == ["muse-spark-1.3"]

    # 2. Born-digital PDF with inconsistent arithmetic returns None instead of trusting bad columns.
    spec = _spec("saebyeol", "KRW", "110-482-913746")
    broken = Statement(
        account_id=spec.account_id,
        year=2025,
        month=3,
        bank=SAEBYEOL,
        opening_balance=10_000_000,
        closing_balance=99_999_999,  # contradicts opening + txn
        txns=(Txn("t1", date(2025, 3, 5), "Transfer", 1_000_000, 11_000_000, "routine"),),
    )
    broken_pdf = tmp_path / "broken.pdf"
    render_statement(spec, broken, FONTS, broken_pdf)
    assert read_digital_pdf_page(broken_pdf, page=1) is None


def test_photo_runner_ingests_born_digital_pdf_and_csv_offline_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    spec = _spec("saebyeol", "KRW", "110-482-913746")
    stmt = Statement(
        account_id=spec.account_id,
        year=2025,
        month=1,
        bank=SAEBYEOL,
        opening_balance=20_000_000,
        closing_balance=30_000_000,
        txns=(Txn("t1", date(2025, 1, 15), "Deposit", 10_000_000, 30_000_000, "routine"),),
    )
    pdf_path = tmp_path / "kr_2025-01.pdf"
    render_statement(spec, stmt, FONTS, pdf_path)

    csv_path = tmp_path / "de_2025.csv"
    csv_path.write_text(
        "\ufeffInstitution,Bergwald Bank AG\n"
        "Account Number,DE36990123450481527360\n"
        "Holder,JUNG SEULGI JOY\n"
        "Currency,EUR\n"
        "Period,2025-01-01 ~ 2025-12-31\n"
        "Opening Balance,15000.00\n"
        "Closing Balance,16500.00\n"
        "Date,Description,Debit,Credit,Balance\n"
        "2025-04-10,Salary,,2000.00,17000.00\n"
        "2025-09-01,Rent,500.00,,16500.00\n",
        encoding="utf-8",
    )

    intake_path = tmp_path / "intake.json"
    intake_path.write_text(
        json.dumps(
            {
                "client": "Synthetic Client",
                "source": "Synthetic test",
                "accounts": [
                    {"account_number": "110-482-913746", "ownership": "sole", "financial_interest": True},
                    {
                        "account_number": "DE36990123450481527360",
                        "ownership": "sole",
                        "financial_interest": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    review_set = tmp_path / "set"
    res_pdf = photo.run(pdf_path, review_set, synthetic=True, intake_path=intake_path)
    res_csv = photo.run(csv_path, review_set, synthetic=True, intake_path=intake_path)
    assert res_pdf["call"]["model"] == "digital-pdf"
    assert res_pdf["call"]["cost_usd"] == 0.0
    assert res_csv["call"]["model"] == "digital-csv"
    assert res_csv["call"]["cost_usd"] == 0.0

    ws = build(Inputs(review_set, review_set / "readings.json", review_set / "intake.json"))
    by_acct = {a["key"]: a for a in ws["accounts"]}
    assert by_acct["110482913746"]["max"]["amount"] == "30000000"
    assert by_acct["DE36990123450481527360"]["max"]["amount"] == "17000.00"
    assert not [f for f in ws["flags"] if f["code"] == "photo_location_unverified"]


@pytest.mark.parametrize(
    ("csv_body", "err_match"),
    [
        ("foo,bar\n1,2\n", "no recognizable date and amount"),
        ("Date,Debit,Credit\n2025-01-02,100,200\n", "both debit and credit"),
        ("Date,Amount,Currency\n2025-01-02,100,EUR\n2025-01-03,100,KRW\n", "multiple currencies"),
        ("Currency,EUR\nDate,Amount,Currency\n2025-01-02,100,KRW\n", "multiple currencies"),
        (
            "Opening Balance,100.00\nClosing Balance,999.00\nDate,Amount\n2025-01-02,50.00\n",
            "reconciliation",
        ),
        (
            "Date,Amount,Balance\n2025-01-01,10,100\n2025-01-02,20,\n2025-01-03,30,130\n",
            "reconciliation",
        ),
    ],
)
def test_csv_statement_rejects_ambiguous_or_unreconciled_files(tmp_path, csv_body, err_match):
    p = tmp_path / "bad.csv"
    p.write_text(csv_body, encoding="utf-8")
    with pytest.raises(ValueError, match=err_match):
        read_csv_statement(p)


def test_csv_zero_padded_debit_credit_and_sparse_running_balances_reconcile(tmp_path):
    p = tmp_path / "zero_col.csv"
    p.write_text(
        "Currency,EUR\n"
        "Closing Balance,150.00\n"
        "Date,Debit,Credit,Balance\n"
        "2025-01-01,0.00,10.00,100.00\n"
        "2025-01-02,20.00,0.00,\n"
        "2025-01-03,0.00,70.00,150.00\n",
        encoding="utf-8",
    )
    r = read_csv_statement(p)
    assert [row["amount_column"] for row in r["rows"]] == ["credit", "debit", "credit"]
    assert [row["amount_text"] for row in r["rows"]] == ["10.00", "20.00", "70.00"]


def test_csv_unpadded_decimals_multi_account_balances_and_preview_direction(tmp_path):
    from decimal import Decimal

    from cross_border_fbar.extract.digital import render_csv_preview
    from cross_border_fbar.reconcile.observe import observe

    # 1. Unpadded 1-decimal CSV amounts normalize cleanly rather than becoming 10x larger, while junk rejects.
    dec_csv = tmp_path / "unpadded.csv"
    dec_csv.write_text(
        "Currency,EUR\n"
        "Opening Balance,10.5\n"
        "Closing Balance,12.0\n"
        "Date,Amount\n"
        "2025-01-01,1.5\n"
        "2025-01-02,+0.5\n"
        "2025-01-03,-0.5\n",
        encoding="utf-8",
    )
    dec_reading = read_csv_statement(dec_csv)
    obs = observe(dec_reading, "unpadded.csv")
    assert obs.opening == Decimal("10.50")
    assert obs.closing == Decimal("12.00")
    assert [ln.amount for ln in obs.lines] == [Decimal("1.50"), Decimal("0.50"), Decimal("-0.50")]

    for bad_val in ("12abc", "1.2345"):
        bad_csv = tmp_path / f"bad_{bad_val}.csv"
        bad_csv.write_text(f"Date,Amount\n2025-01-01,{bad_val}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="unparseable"):
            read_csv_statement(bad_csv)

    # 2. Multi-account CSV reconciles running balances per account and renders row account in preview.
    multi_csv = tmp_path / "multi_acct.csv"
    multi_csv.write_text(
        "Currency,EUR\n"
        "Date,Account,Amount,Balance\n"
        "2025-01-01,DE111,10.00,110.00\n"
        "2025-01-01,DE222,10.00,110.00\n"
        "2025-01-03,DE111,5.00,115.00\n",
        encoding="utf-8",
    )
    multi_reading = read_csv_statement(multi_csv)
    assert len(multi_reading["rows"]) == 3
    multi_png = tmp_path / "multi-p1.png"
    multi_rendered = render_csv_preview(multi_reading, multi_png)
    with Image.open(multi_png) as mimg:
        mw, mh = mimg.size
        rb0 = multi_rendered["rows"][0]["row_bbox"]
        rb1 = multi_rendered["rows"][1]["row_bbox"]
        row_crop_0 = mimg.crop(
            (
                rb0["x_min"] * mw // 1000,
                rb0["y_min"] * mh // 1000,
                rb0["x_max"] * mw // 1000,
                rb0["y_max"] * mh // 1000,
            )
        )
        row_crop_1 = mimg.crop(
            (
                rb1["x_min"] * mw // 1000,
                rb1["y_min"] * mh // 1000,
                rb1["x_max"] * mw // 1000,
                rb1["y_max"] * mh // 1000,
            )
        )
        assert row_crop_0.tobytes() != row_crop_1.tobytes()

    # Rotated born-digital PDF pages conservatively return None so source highlights never mirror.
    import pypdfium2 as pdfium

    spec = _spec("saebyeol", "KRW", "110-482-913746")
    rot_stmt = Statement(
        account_id=spec.account_id,
        year=2025,
        month=3,
        bank=SAEBYEOL,
        opening_balance=10_000_000,
        closing_balance=11_000_000,
        txns=(Txn("t1", date(2025, 3, 5), "Transfer", 1_000_000, 11_000_000, "routine"),),
    )
    unrotated_pdf = tmp_path / "unrot.pdf"
    rotated_pdf = tmp_path / "rot180.pdf"
    render_statement(spec, rot_stmt, FONTS, unrotated_pdf)
    doc = pdfium.PdfDocument(str(unrotated_pdf))
    try:
        doc[0].set_rotation(180)
        doc.save(str(rotated_pdf))
    finally:
        doc.close()
    assert read_digital_pdf_page(rotated_pdf, page=1) is None

    broken_multi = tmp_path / "broken_multi.csv"
    broken_multi.write_text(
        "Currency,EUR\n"
        "Date,Account,Amount,Balance\n"
        "2025-01-01,DE111,10.00,110.00\n"
        "2025-01-02,DE222,20.00,220.00\n"
        "2025-01-03,DE111,5.00,999.00\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reconciliation"):
        read_csv_statement(broken_multi)

    # 3. Rendered CSV preview distinguishes debit vs credit direction inside the highlighted amount region.
    dir_csv = tmp_path / "dir.csv"
    dir_csv.write_text(
        "Currency,EUR\n"
        "Date,Description,Debit,Credit,Balance\n"
        "2025-02-01,Transfer A,100.00,,900.00\n"
        "2025-02-02,Transfer A,,100.00,1000.00\n",
        encoding="utf-8",
    )
    dir_reading = read_csv_statement(dir_csv)
    preview_png = tmp_path / "dir-p1.png"
    rendered = render_csv_preview(dir_reading, preview_png)
    with Image.open(preview_png) as img:
        w, h = img.size
        b0 = rendered["rows"][0]["amount_bbox"]
        b1 = rendered["rows"][1]["amount_bbox"]
        crop_dr = img.crop(
            (
                b0["x_min"] * w // 1000,
                b0["y_min"] * h // 1000,
                b0["x_max"] * w // 1000,
                b0["y_max"] * h // 1000,
            )
        )
        crop_cr = img.crop(
            (
                b1["x_min"] * w // 1000,
                b1["y_min"] * h // 1000,
                b1["x_max"] * w // 1000,
                b1["y_max"] * h // 1000,
            )
        )
        assert crop_dr.tobytes() != crop_cr.tobytes()
