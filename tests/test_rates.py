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
    doc = json.loads(rates.rates_path(2025).read_text(encoding="utf-8"))
    doc["record_date"] = "2025-09-30"
    bad = tmp_path / "rates.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError):
        rates.load(2025, bad)


def test_fetch_writes_workspace_file_without_modifying_packaged_rates(tmp_path, monkeypatch, capsys):
    bundled = rates.rates_path(2025)
    original = bundled.read_bytes()
    doc = json.loads(original)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rates, "fetch", lambda year, currencies: doc)
    rates.main(["fetch", "2025", "KRW"])
    output = tmp_path / "data" / "treasury_rates_2025.json"
    assert output.is_file()
    assert rates.load(2025, output)["KRW"] == rates.load(2025)["KRW"]
    assert bundled.read_bytes() == original
    assert "Load explicitly with rates.load(2025, path=Path(" in capsys.readouterr().out
