import json
from decimal import Decimal

import pytest

from cross_border_fbar import rates


def test_committed_2025_rates_load_as_decimal_with_source():
    loaded = rates.load(2025)
    assert set(loaded) >= {"KRW", "EUR", "THB"}
    for r in loaded.values():
        assert isinstance(r.units_per_usd, Decimal) and r.units_per_usd > 0
        assert r.record_date == "2025-12-31"
        assert r.source_url.startswith(rates.API_URL)


def test_query_filters_on_year_end_and_treasury_labels():
    url = rates.query_url(2025, ["KRW", "EUR"])
    assert "record_date:eq:2025-12-31" in url
    assert "Korea-Won" in url and "Euro" in url


def test_load_rejects_a_file_for_another_date(tmp_path):
    doc = json.loads(rates.rates_path(2025).read_text())
    doc["record_date"] = "2025-09-30"
    bad = tmp_path / "rates.json"
    bad.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        rates.load(2025, bad)
