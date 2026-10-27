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
from .household import household
from .ledger import build_ledger, monthly_statements
from .render import LAYOUTS, render_statement
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
        print(
            f"  {acct['account_id']:12} max {acct['max_value']['amount']:>14} {acct['currency']} "
            f"= ${acct['max_value_usd']:,}  flags: {', '.join(codes) or '-'}"
        )
    h = truth["household"]
    print(f"  aggregate ${h['aggregate_max_usd']:,} (month-end only ${h['aggregate_month_end_only_usd']:,})")


if __name__ == "__main__":
    main()
