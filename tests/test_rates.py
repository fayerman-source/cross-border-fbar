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


def test_load_available_merges_workspace_rates_and_supports_other_tax_years(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws_data = tmp_path / "data"
    ws_data.mkdir()
    inr_2025 = {
        "tax_year": 2025,
        "record_date": "2025-12-31",
        "basis": "foreign currency units per 1 USD",
        "source": {"api_query": f"{rates.API_URL}?synthetic=1"},
        "rates": {
            "INR": {"treasury_label": "India-Rupee", "units_per_usd": "85.50", "effective_date": "2025-12-31"}
        },
    }
    (ws_data / "treasury_rates_2025.json").write_text(json.dumps(inr_2025), encoding="utf-8")
    merged = rates.load_available(2025)
    assert {"KRW", "EUR", "THB", "INR"} <= set(merged)
    assert merged["INR"].units_per_usd == Decimal("85.50")

    rates_2024 = {
        "tax_year": 2024,
        "record_date": "2024-12-31",
        "basis": "foreign currency units per 1 USD",
        "source": {"api_query": f"{rates.API_URL}?synthetic=2024"},
        "rates": {
            "EUR": {
                "treasury_label": "Euro Zone-Euro",
                "units_per_usd": "0.96",
                "effective_date": "2024-12-31",
            }
        },
    }
    (ws_data / "treasury_rates_2024.json").write_text(json.dumps(rates_2024), encoding="utf-8")
    assert rates.load_available(2024)["EUR"].units_per_usd == Decimal("0.96")
    assert rates.load_available(2023) == {}


def test_eval_limit_writes_separate_output_and_app_accepts_tax_year_and_rates(tmp_path, monkeypatch):
    from cross_border_fbar.app import server
    from cross_border_fbar.eval import __main__ as eval_main

    fake_results = tmp_path / "results"
    fake_results.mkdir()
    full_file = fake_results / "muse-spark-1.3_synth.json"
    full_file.write_text('{"pages": ["keep"]}', encoding="utf-8")
    monkeypatch.setattr(eval_main, "RESULTS", fake_results)
    monkeypatch.setattr(
        eval_main,
        "evaluate",
        lambda *a, **k: {
            "model": "muse-spark-1.3",
            "prompt": "statement_page_v3",
            "mode": "replay",
            "cost_usd_this_run": 0,
            "errors": [],
            "summary": {"all": {"pages": 1}},
            "pages": [{}],
        },
    )
    monkeypatch.setattr("sys.argv", ["eval", "--set", str(tmp_path / "synth"), "--limit", "1"])
    eval_main.main()
    assert json.loads(full_file.read_text(encoding="utf-8")) == {"pages": ["keep"]}
    assert (fake_results / "muse-spark-1.3_synth_limit1.json").is_file()

    seen = {}

    class FakeReview:
        def __init__(self, inputs, review_dir):
            seen["tax_year"] = inputs.tax_year
            seen["rates_path"] = inputs.rates_path

    class FakeServer:
        def __init__(self, addr, handler):
            pass

        def serve_forever(self):
            pass

    custom_rates = tmp_path / "custom_rates.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "Review", FakeReview)
    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(
        "sys.argv",
        ["app", "--tax-year", "2024", "--rates", str(custom_rates)],
    )
    server.main()
    assert seen == {"tax_year": 2024, "rates_path": custom_rates}


def test_multi_year_rate_loading_cli_range_fetch_and_lookback_reconciliation(tmp_path, monkeypatch):
    from datetime import date

    from cross_border_fbar.reconcile.accounts import Intake
    from cross_border_fbar.reconcile.fbar import reconcile_years
    from cross_border_fbar.reconcile.observe import Line, Observation, Source

    # 1. CLI multi-year range + --all uses mocked fetch (zero network calls).
    fetched_calls: list[tuple[int, int]] = []

    def fake_fetch(year: int, currencies: list[str]) -> dict:
        fetched_calls.append((year, len(currencies)))
        return {
            "tax_year": year,
            "record_date": f"{year}-12-31",
            "basis": "foreign currency units per 1 USD",
            "source": {"api_query": f"{rates.API_URL}?year={year}"},
            "rates": {
                "CAD": {
                    "treasury_label": "Canada-Dollar",
                    "units_per_usd": "1.35" if year == 2023 else "1.44",
                    "effective_date": f"{year}-12-31",
                }
            },
        }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rates, "fetch", fake_fetch)
    rates.main(["fetch", "2023..2024", "--all"])
    assert fetched_calls == [(2023, len(rates.TREASURY_LABELS)), (2024, len(rates.TREASURY_LABELS))]

    by_year = rates.load_years([2022, 2023, 2024, 2025])
    assert by_year[2022] == {}  # missing year stays empty rather than borrowing another year's rate
    assert by_year[2023]["CAD"].units_per_usd == Decimal("1.35")
    assert by_year[2024]["CAD"].units_per_usd == Decimal("1.44")
    assert "KRW" in by_year[2025]

    # 2. Multi-year reconciliation converts 2023 and 2024 at each year's Dec 31 rate and flags 2022.
    def make_obs(year: int) -> Observation:
        src = Source(f"cad-{year}.png", "opening_balance", None)
        return Observation(
            upload=f"cad-{year}.png",
            institution="Maple Bank",
            currency="CAD",
            account_key="CAD001",
            account_number="CAD-001",
            holder="Synthetic Holder",
            period_start=date(year, 1, 1),
            period_end=date(year, 12, 31),
            opening=Decimal("14000.00"),
            closing=Decimal("14000.00"),
            lines=[Line(0, date(year, 6, 1), Decimal("0.00"), Decimal("14000.00"), "hold", src)],
            sources={"opening_balance": src, "closing_balance": src},
        )

    lookback = reconcile_years(
        [make_obs(2022), make_obs(2023), make_obs(2024)],
        {"CAD001": Intake("sole", True)},
        [2022, 2023, 2024],
        by_year,
    )
    accts_2022, totals_2022 = lookback[2022]
    assert accts_2022[0].max_value_usd is None
    assert any(f.code == "currency_unknown" for f in accts_2022[0].flags)
    assert totals_2022.above_aggregate_threshold is None

    accts_2023, totals_2023 = lookback[2023]
    accts_2024, totals_2024 = lookback[2024]
    # 14,000 / 1.35 -> ceil $10,371 (> $10,000); 14,000 / 1.44 -> ceil $9,723 (<= $10,000)
    assert accts_2023[0].max_value_usd == 10371
    assert totals_2023.above_aggregate_threshold is True
    assert accts_2024[0].max_value_usd == 9723
    assert totals_2024.above_aggregate_threshold is False

    # 3. Partial-period pages with one readable boundary retain their year's closing/opening evidence.
    partial_src = Source("usd-partial.png", "closing_balance", None)
    partial_2024 = Observation(
        upload="usd-partial.png",
        institution="Harbor Bank",
        currency="USD",
        account_key="USD999",
        account_number="USD-999",
        holder="Synthetic Holder",
        period_start=None,
        period_end=date(2024, 12, 31),
        opening=None,
        closing=Decimal("20000.00"),
        lines=[Line(0, date(2023, 11, 15), Decimal("100.00"), Decimal("500.00"), "prior", partial_src)],
        sources={"closing_balance": partial_src},
    )
    partial_lookback = reconcile_years([partial_2024], {}, [2024], by_year)
    p_accts_2024, p_totals_2024 = partial_lookback[2024]
    assert len(p_accts_2024) == 1
    assert p_accts_2024[0].max_value_usd == 20000
    assert p_accts_2024[0].year_end_balance == Decimal("20000.00")
    assert p_totals_2024.above_aggregate_threshold is True
