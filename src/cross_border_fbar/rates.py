"""Treasury Reporting Rates of Exchange: fetch once, store with the source, load as Decimal.

FBAR converts each account's maximum at the Treasury rate for the last day of the
year (https://www.fincen.gov/reporting-maximum-account-value). Rates are never
typed in or recalled by a model: they come from Treasury's API and are saved with
the exact query URL and fetch time.

    python -m cross_border_fbar.rates fetch 2025 KRW EUR THB

Bundled rates are read from the installed package. Fetch writes a separate file under the current
workspace's data/ directory; pass that path to load() to use it without modifying the installation.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable
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
TREASURY_LABELS = {
    "AED": "United Arab Emirates-Dirham",
    "AUD": "Australia-Dollar",
    "BHD": "Bahrain-Dinar",
    "BRL": "Brazil-Real",
    "CAD": "Canada-Dollar",
    "CHF": "Switzerland-Franc",
    "CNY": "China-Renminbi",
    "DKK": "Denmark-Krone",
    "EUR": "Euro Zone-Euro",
    "GBP": "United Kingdom-Pound",
    "HKD": "Hong Kong-Dollar",
    "IDR": "Indonesia-Rupiah",
    "ILS": "Israel-Shekel",
    "INR": "India-Rupee",
    "JOD": "Jordan-Dinar",
    "JPY": "Japan-Yen",
    "KRW": "Korea-Won",
    "KWD": "Kuwait-Dinar",
    "MXN": "Mexico-Peso",
    "MYR": "Malaysia-Ringgit",
    "NOK": "Norway-Krone",
    "NZD": "New Zealand-Dollar",
    "PHP": "Philippines-Peso",
    "PLN": "Poland-Zloty",
    "SAR": "Saudi Arabia-Riyal",
    "SEK": "Sweden-Krona",
    "SGD": "Singapore-Dollar",
    "THB": "Thailand-Baht",
    "TWD": "Taiwan-Dollar",
    "VND": "Vietnam-Dong",
    "ZAR": "South Africa-Rand",
}
DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Rate:
    currency: str
    units_per_usd: Decimal  # Treasury quotes foreign units per 1 USD
    record_date: str
    source_url: str
    basis: str = "treasury"  # treasury | cpa | identity


def rates_path(year: int) -> Path:
    return DATA_DIR / f"treasury_rates_{year}.json"


def workspace_rates_path(year: int) -> Path:
    return Path.cwd() / "data" / f"treasury_rates_{year}.json"


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
    doc = json.loads((path or rates_path(year)).read_text(encoding="utf-8"))
    if doc["record_date"] != f"{year}-12-31":
        raise ValueError(f"rates file is for {doc['record_date']}, not {year}-12-31")
    return {
        c: Rate(c, Decimal(r["units_per_usd"]), doc["record_date"], doc["source"]["api_query"])
        for c, r in doc["rates"].items()
    }


def load_available(year: int, path: Path | None = None) -> dict[str, Rate]:
    """Load an explicit rates file, or merge bundled and workspace rates for year when present."""
    if path is not None:
        return load(year, path)
    merged: dict[str, Rate] = {}
    if rates_path(year).is_file():
        merged.update(load(year))
    ws_path = workspace_rates_path(year)
    if ws_path.is_file():
        merged.update(load(year, ws_path))
    return merged


def load_years(years: Iterable[int], directory: Path | None = None) -> dict[int, dict[str, Rate]]:
    """Load available Dec 31 Treasury rates for each tax year in a multi-year lookback.

    Never invents or carries forward another year's exchange rate: if a year has no bundled or
    workspace file, its mapping is empty so reconciliation raises `currency_unknown` for review.
    """
    result: dict[int, dict[str, Rate]] = {}
    for year in years:
        merged: dict[str, Rate] = {}
        if rates_path(year).is_file():
            merged.update(load(year))
        base_dir = directory if directory is not None else (Path.cwd() / "data")
        candidate = base_dir / f"treasury_rates_{year}.json"
        if candidate.is_file():
            merged.update(load(year, candidate))
        result[year] = merged
    return result


def _parse_years(spec: str) -> list[int]:
    if ".." in spec:
        lo_s, hi_s = spec.split("..", 1)
        lo, hi = int(lo_s), int(hi_s)
        if hi < lo:
            raise ValueError(f"reversed year range {spec!r}")
        return list(range(lo, hi + 1))
    return [int(part) for part in spec.split(",")]


def main(argv: list[str]) -> None:
    if len(argv) < 3 or argv[0] != "fetch":
        sys.exit("usage: python -m cross_border_fbar.rates fetch <year|start..end> <--all | ISO ...>")
    years = _parse_years(argv[1])
    currencies = sorted(TREASURY_LABELS) if argv[2:] == ["--all"] else argv[2:]
    for year in years:
        doc = fetch(year, currencies)
        # Fetching writes to the current workspace, never into a potentially read-only installation.
        out = Path.cwd() / "data" / f"treasury_rates_{year}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}: " + ", ".join(f"{c} {r['units_per_usd']}" for c, r in doc["rates"].items()))
        print(f"Load explicitly with rates.load({year}, path=Path({str(out)!r})).")


if __name__ == "__main__":
    main(sys.argv[1:])
