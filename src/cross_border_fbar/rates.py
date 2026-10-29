"""Treasury Reporting Rates of Exchange: fetch once, store with the source, load as Decimal.

FBAR converts each account's maximum at the Treasury rate for the last day of the
year (https://www.fincen.gov/reporting-maximum-account-value). Rates are never
typed in or recalled by a model: they come from Treasury's API and are saved with
the exact query URL and fetch time.

    python -m cross_border_fbar.rates fetch 2025 KRW EUR THB
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

API_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/rates_of_exchange"
DATASET_PAGE = (
    "https://fiscaldata.treasury.gov/datasets/treasury-reporting-rates-exchange/"
    "treasury-reporting-rates-of-exchange"
)
# ISO code -> Treasury's country_currency_desc label.
TREASURY_LABELS = {"KRW": "Korea-Won", "EUR": "Euro Zone-Euro", "THB": "Thailand-Baht", "INR": "India-Rupee"}
DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@dataclass(frozen=True)
class Rate:
    currency: str
    units_per_usd: Decimal  # Treasury quotes foreign units per 1 USD
    record_date: str
    source_url: str
    basis: str = "treasury"  # treasury | cpa | identity


def rates_path(year: int) -> Path:
    return DATA_DIR / f"treasury_rates_{year}.json"


def query_url(year: int, currencies: list[str]) -> str:
    labels = ",".join(TREASURY_LABELS[c] for c in currencies)
    params = {
        "fields": "record_date,country_currency_desc,exchange_rate,effective_date",
        "filter": f"record_date:eq:{year}-12-31,country_currency_desc:in:({labels})",
    }
    return f"{API_URL}?{urllib.parse.urlencode(params, safe=':,()')}"


def fetch(year: int, currencies: list[str]) -> dict:
    url = query_url(year, currencies)
    with urllib.request.urlopen(url, timeout=30) as resp:
        rows = json.loads(resp.read())["data"]
    by_label = {r["country_currency_desc"]: r for r in rows}
    missing = [c for c in currencies if TREASURY_LABELS[c] not in by_label]
    if missing:
        # Never fall back to a remembered or guessed rate.
        raise LookupError(f"Treasury has no {year}-12-31 rate for {missing}")
    return {
        "tax_year": year,
        "record_date": f"{year}-12-31",
        "basis": "foreign currency units per 1 USD",
        "source": {
            "api_query": url,
            "dataset_page": DATASET_PAGE,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "rates": {
            c: {
                "treasury_label": TREASURY_LABELS[c],
                "units_per_usd": by_label[TREASURY_LABELS[c]]["exchange_rate"],
                "effective_date": by_label[TREASURY_LABELS[c]]["effective_date"],
            }
            for c in currencies
        },
    }


def load(year: int, path: Path | None = None) -> dict[str, Rate]:
    doc = json.loads((path or rates_path(year)).read_text())
    if doc["record_date"] != f"{year}-12-31":
        raise ValueError(f"rates file is for {doc['record_date']}, not {year}-12-31")
    return {
        c: Rate(c, Decimal(r["units_per_usd"]), doc["record_date"], doc["source"]["api_query"])
        for c, r in doc["rates"].items()
    }


def main(argv: list[str]) -> None:
    if len(argv) < 3 or argv[0] != "fetch":
        sys.exit("usage: python -m cross_border_fbar.rates fetch <year> <ISO> [<ISO> ...]")
    year, currencies = int(argv[1]), argv[2:]
    doc = fetch(year, currencies)
    out = rates_path(year)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {out}: " + ", ".join(f"{c} {r['units_per_usd']}" for c, r in doc["rates"].items()))


if __name__ == "__main__":
    main(sys.argv[1:])
