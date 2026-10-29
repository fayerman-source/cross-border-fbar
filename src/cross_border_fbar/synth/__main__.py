"""Generate the synthetic household's statements and answer key.

    python -m cross_border_fbar.synth --out out/synth [--seed 2025] [--png] [--threshold-usd 1000]

Writes statements/*.pdf, optional pages/*.png at a fixed DPI, and truth.json.
Same seed, same bytes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import rates
from .compiled import MASKED_CHECKING, MMDA_NUMBER, PERIOD, compiled_rows, mmda_true_peak
from .household import household
from .ledger import build_ledger, monthly_statements
from .render import LAYOUTS, render_compiled, render_statement
from .truth import account_truth, household_totals, major, to_usd_ceiling

TAX_YEAR = 2025
FONTS = Path(__file__).resolve().parents[3] / "fonts"
GENERATOR_VERSION = "0.1.0"


def rasterize(pdf: Path, out_dir: Path, dpi: int) -> list[dict]:
    import pypdfium2 as pdfium

    pages = []
    doc = pdfium.PdfDocument(str(pdf))
    for i in range(len(doc)):
        image = doc[i].render(scale=dpi / 72).to_pil()
        path = out_dir / f"{pdf.stem}-p{i + 1}.png"
        image.save(path)
        pages.append(
            {
                "page": i + 1,
                "file": str(path.relative_to(out_dir.parent)),
                "width_px": image.width,
                "height_px": image.height,
                "dpi": dpi,
            }
        )
    doc.close()
    return pages


def _compiled_statement(out: Path, seed: int, png: bool, dpi: int, accounts: list[dict]) -> dict:
    """The compiled multi-account list (no balances), plus answer-key entries for its two accounts."""
    checking = build_ledger(household(TAX_YEAR)[0], TAX_YEAR, seed)
    rows = compiled_rows(seed, checking)
    pdf = out / "statements" / "compiled-ledger.pdf"
    drawn = render_compiled(rows, PERIOD, FONTS, pdf)
    accounts.append(
        {
            "account_id": "kr-mmda",
            "currency": "KRW",
            "account_number": MMDA_NUMBER,
            "holder_printed": None,
            "ownership": "sole",
            "financial_interest": True,
            "owners": ["JUNG SEULGI JOY"],
            "institutions": [],
            "statements_provided": ["compiled-ledger"],
            "statements_withheld": [],
            "max_value": None,
            "max_value_usd": None,
            "max_month_end": None,
            "intraday_candidates": [],
            "year_end_balance": None,
            "hidden_true_peak": {
                "amount": major(mmda_true_peak(rows, TAX_YEAR), "KRW"),
                "note": "from the hidden ledger; no document shows any balance for this account",
            },
            "expected_flags": [{"code": "no_balance_anchor", "severity": "review"}],
        }
    )
    accounts.append(
        {
            "account_id": "masked-checking",
            "currency": "KRW",
            "account_number": MASKED_CHECKING,
            "holder_printed": None,
            "ownership": "unknown",
            "financial_interest": None,
            "owners": [],
            "institutions": [],
            "statements_provided": ["compiled-ledger"],
            "statements_withheld": [],
            "max_value": None,
            "max_value_usd": None,
            "max_month_end": None,
            "intraday_candidates": [],
            "year_end_balance": None,
            "hidden_true_peak": None,
            "counted": False,
            "expected_flags": [{"code": "account_number_masked", "severity": "review"}],
        }
    )
    rec = {
        "statement_id": "compiled-ledger",
        "account_id": None,
        "period": PERIOD[0].isoformat(),
        "file": str(pdf.relative_to(out)),
        "bank": None,
        "layout": "kr_ledger",
        "pages": drawn["pages"],
        "prints_running_balance": False,
        "fields": drawn["fields"],
        "transactions": [
            {
                "txn_id": r.row_id,
                "date": r.day.isoformat(),
                "description": r.counterparty,
                "amount": major(r.signed, "KRW"),
                "balance_after": None,
                "balance_printed": False,
                "account_printed": r.account_printed,
                "tag": r.account_id,
                **drawn["lines"][r.row_id],
            }
            for r in rows
        ],
    }
    if png:
        (out / "pages").mkdir(parents=True, exist_ok=True)
        rec["page_images"] = rasterize(pdf, out / "pages", dpi)
    return rec


def generate(out: Path, seed: int, png: bool, dpi: int, threshold_usd: int) -> dict:
    year_rates = rates.load(TAX_YEAR)
    rates_doc = json.loads(rates.rates_path(TAX_YEAR).read_text())
    accounts, statements_out, month_end_usd = [], [], {}
    for spec in household(TAX_YEAR):
        ledger = build_ledger(spec, TAX_YEAR, seed)
        statements = monthly_statements(spec, TAX_YEAR, ledger)
        prints = {s.statement_id: LAYOUTS[s.bank.layout].prints_running_balance for s in statements}
        rate = year_rates[spec.currency]
        acct = account_truth(spec, ledger, statements, rate, prints, threshold_usd)
        accounts.append(acct)
        provided = [s for s in statements if s.month not in spec.missing_months]
        month_end_usd[spec.account_id] = to_usd_ceiling(
            max(s.closing_balance for s in provided), spec.currency, rate
        )
        for s in provided:
            pdf = out / "statements" / f"{s.statement_id}.pdf"
            drawn = render_statement(spec, s, FONTS, pdf)
            rec = {
                "statement_id": s.statement_id,
                "account_id": spec.account_id,
                "period": f"{TAX_YEAR}-{s.month:02d}",
                "file": str(pdf.relative_to(out)),
                "bank": s.bank.name,
                "layout": s.bank.layout,
                "pages": drawn["pages"],
                "prints_running_balance": drawn["prints_running_balance"],
                "fields": {
                    k: {
                        **v,
                        "value": major(v["value"], spec.currency)
                        if isinstance(v["value"], int)
                        else v["value"],
                    }
                    for k, v in drawn["fields"].items()
                },
                "transactions": [
                    {
                        "txn_id": t.txn_id,
                        "date": t.day.isoformat(),
                        "description": t.description,
                        "amount": major(t.amount, spec.currency),
                        "balance_after": major(t.balance_after, spec.currency),
                        "balance_printed": drawn["prints_running_balance"],
                        "tag": t.tag,
                        **drawn["lines"][t.txn_id],
                    }
                    for t in s.txns
                ],
            }
            if png:
                (out / "pages").mkdir(parents=True, exist_ok=True)
                rec["page_images"] = rasterize(pdf, out / "pages", dpi)
            statements_out.append(rec)

    statements_out.append(_compiled_statement(out, seed, png, dpi, accounts))

    truth = {
        "schema_version": 1,
        "synthetic": True,
        "notice": "Fictional household, banks and accounts. Not a real person's statements.",
        "generator": {"version": GENERATOR_VERSION, "seed": seed},
        "tax_year": TAX_YEAR,
        "client": {
            "name": "Joy Seulgi Jung",
            "name_hangul": "정슬기",
            "us_person": True,
            "residence": "Seoul, Republic of Korea",
        },
        "exchange_rates": rates_doc,
        "bbox_convention": "0-1000 grid of the page, origin top-left: x0, y0, x1, y1",
        "accounts": accounts,
        "household": household_totals(accounts, month_end_usd, threshold_usd),
        "statements": statements_out,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n")
    # What the client states on the intake call (ownership isn't printed on statements). The worksheet
    # app reads this, never the answer key; voice intake will produce the same shape.
    intake = {
        "client": truth["client"]["name"],
        "source": "synthetic intake call script (fictional)",
        "accounts": [
            {
                "account_number": a["account_number"],
                "ownership": a["ownership"],
                "financial_interest": a["financial_interest"],
            }
            for a in truth["accounts"]
            if a.get("counted", True)
        ],
    }
    (out / "intake.json").write_text(json.dumps(intake, ensure_ascii=False, indent=2) + "\n")
    return truth


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=Path("out/synth"))
    p.add_argument("--seed", type=int, default=2025)
    p.add_argument("--png", action="store_true", help="also rasterize every page")
    p.add_argument("--dpi", type=int, default=150)
    p.add_argument("--threshold-usd", type=int, default=1000)
    a = p.parse_args()
    truth = generate(a.out, a.seed, a.png, a.dpi, a.threshold_usd)
    print(f"{len(truth['statements'])} statements -> {a.out}")
    for acct in truth["accounts"]:
        codes = sorted({f["code"] for f in acct["expected_flags"]})
        value = (
            f"{acct['max_value']['amount']:>14} {acct['currency']} = ${acct['max_value_usd']:,}"
            if acct["max_value"]
            else f"{'unknown':>14} {acct['currency']}"
        )
        print(f"  {acct['account_id']:15} max {value}  flags: {', '.join(codes) or '-'}")
    h = truth["household"]
    print(f"  aggregate ${h['aggregate_max_usd']:,} (month-end only ${h['aggregate_month_end_only_usd']:,})")


if __name__ == "__main__":
    main()
