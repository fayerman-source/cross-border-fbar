"""Edge cases from an independent review of the reconciliation (PR #3). Each one failed before its fix."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from cross_border_fbar import rates
from cross_border_fbar.reconcile.accounts import Flag, Intake, reconcile
from cross_border_fbar.reconcile.fbar import fbar_totals
from cross_border_fbar.reconcile.flags import CATALOG, FlagDefinition
from cross_border_fbar.reconcile.observe import Line, Observation, Source, observe, parse_currency

RATES = rates.load(2025)
SOLE = {"ACC1": Intake("sole", True)}


def obs(upload, start, end, opening, closing, lines=(), key="ACC1", currency="EUR", problems=()):
    def src(ref):
        return Source(upload, ref, None)

    return Observation(
        upload,
        "Bank",
        currency,
        key,
        key,
        "Holder",
        start,
        end,
        opening,
        closing,
        [Line(i, d, a, b, "", src(f"row:{i}")) for i, (d, a, b) in enumerate(lines)],
        {n: src(n) for n in ("opening_balance", "closing_balance", "account_number", "period", "currency")},
        list(problems),
    )


def year_of(amount=D(1000), key="ACC1"):
    return [obs(f"{key}-{m}", date(2025, m, 1), _month_end(m), amount, amount, key=key) for m in range(1, 13)]


def _month_end(m):
    return date(2025 + m // 12, m % 12 + 1, 1) - timedelta(days=1)


def codes(result):
    return {f.code for f in result.flags}


def test_balances_outside_the_tax_year_do_not_count():
    jan26 = obs(
        "jan26",
        date(2026, 1, 1),
        date(2026, 1, 31),
        D(1000),
        D(1000),
        [(date(2026, 1, 10), D(60000), D(61000))],
    )
    dec24 = obs("dec24", date(2024, 12, 1), date(2024, 12, 31), D(50000), D(1000))
    (r,) = reconcile([*year_of(), jan26, dec24], SOLE, RATES, 2025)
    assert r.max_value.amount == D(1000)
    assert "out_of_year_statement" in codes(r)


def test_pages_missing_key_fields_are_never_merged_as_duplicates():
    a = obs("a", None, None, None, None, [(date(2025, 3, 5), D(20000), D(20000))])
    b = obs("b", None, None, None, None, [(date(2025, 4, 5), D(70000), D(70000))])
    (r,) = reconcile([a, b], SOLE, RATES, 2025)
    assert r.duplicate_uploads == []
    assert r.max_value.amount == D(70000)


def test_an_unreadable_balance_is_a_review_flag():
    june = obs(
        "jun",
        date(2025, 6, 1),
        date(2025, 6, 30),
        None,
        None,
        [(date(2025, 6, 3), D(80000), None), (date(2025, 6, 9), D(-80000), None)],
        problems=["opening_balance", "closing_balance"],
    )
    months = [o for o in year_of() if o.period_start.month != 6]
    (r,) = reconcile([*months, june], SOLE, RATES, 2025)
    assert "field_unreadable" in codes(r)
    # The opening is seeded from May's closing, so the 80,000 peak is found (as computed).
    assert r.max_value.amount == D(81000)
    assert r.max_value.kind == "computed"


def test_a_gap_inside_a_month_is_flagged():
    jan = obs("jan", date(2025, 1, 1), date(2025, 1, 15), D(1000), D(1000))
    rest = [o for o in year_of() if o.period_start.month > 1]
    (r,) = reconcile([jan, *rest], SOLE, RATES, 2025)
    assert "coverage_gap" in codes(r)
    assert "missing_statement" not in codes(r)


def test_unidentified_pages_are_not_counted_in_the_aggregate():
    pages = year_of(D(3000))
    for p in pages[:4]:
        p.account_key = ""
    results = reconcile(pages, SOLE, RATES, 2025)
    totals = fbar_totals(results)
    assert totals.aggregate_max_usd == 3526  # 3000 / 0.851, rounded up, counted once
    assert len(totals.unidentified_uploads) == 4
    assert totals.above_aggregate_threshold is None
    assert "unidentified_accounts_or_pages" in totals.threshold_review_reasons
    assert all("account_unidentified" in codes(r) for r in results if not r.identified)


def test_below_threshold_is_undetermined_with_missing_values_or_review_items():
    (known,) = reconcile(year_of(), SOLE, RATES, 2025)
    assert fbar_totals([known]).above_aggregate_threshold is False
    missing = replace(known, account_number="ACC2", account_key="ACC2", max_value_usd=None, flags=[])
    totals = fbar_totals([known, missing])
    assert totals.above_aggregate_threshold is None
    assert totals.accounts_without_value == ["ACC2"]
    assert "accounts_without_value" in totals.threshold_review_reasons
    gap = replace(known, flags=[Flag("missing_statement", "missing month")])
    assert fbar_totals([gap]).above_aggregate_threshold is None
    assert "review_items" in fbar_totals([gap]).threshold_review_reasons
    above = replace(known, max_value_usd=10001)
    assert fbar_totals([above, missing]).above_aggregate_threshold is True
    benign_review = replace(known, flags=[Flag("institution_names_differ", "two names")])
    assert fbar_totals([benign_review]).above_aggregate_threshold is False
    authority_review = replace(known, flags=[Flag("signature_authority_only", "signature authority")])
    assert fbar_totals([authority_review]).above_aggregate_threshold is False


def test_every_catalog_flag_explicitly_classifies_aggregate_risk():
    from inspect import Parameter, signature

    assert signature(FlagDefinition).parameters["may_raise_aggregate"].default is Parameter.empty
    assert all(type(definition.may_raise_aggregate) is bool for definition in CATALOG.values())
    assert CATALOG["missing_statement"].may_raise_aggregate
    assert CATALOG["arithmetic_mismatch"].may_raise_aggregate
    assert not CATALOG["signature_authority_only"].may_raise_aggregate


def test_a_tie_prefers_the_printed_period_end_over_a_line():
    jan = obs(
        "jan", date(2025, 1, 1), date(2025, 1, 31), D(1000), D(5000), [(date(2025, 1, 5), D(4000), D(5000))]
    )
    (r,) = reconcile([jan], SOLE, RATES, 2025)
    assert r.max_value.source.ref == "closing_balance"
    assert "max_not_at_month_end" not in codes(r)


def test_crossing_uses_the_largest_extra_per_account_not_the_sum():
    base = D(7009)  # $8,237 at 0.851
    days = [
        (date(2025, 3, 5), D(-1277), base - 1277),
        (date(2025, 3, 5), D(1277), base),
        (date(2025, 3, 9), D(-1277), base - 1277),
        (date(2025, 3, 9), D(1277), base),
    ]
    mar = obs("mar", date(2025, 3, 1), date(2025, 3, 31), base, base, days)
    rest = [obs(f"m{m}", date(2025, m, 1), _month_end(m), base, base) for m in range(1, 13) if m != 3]
    (r,) = reconcile([mar, *rest], SOLE, RATES, 2025)
    extras = [f.extra_usd for f in r.flags if f.code == "possible_intraday_peak"]
    assert len(extras) == 2
    assert not fbar_totals([r]).crossing_depends_on_flags  # 8,237 + 1,501 stays under 10,000


def test_rows_in_a_statement_crossing_new_year_get_the_right_year():
    reading = {
        "institution": {"text": "Bank", "status": "read", "bbox": None},
        "currency": {"text": "EUR", "status": "read", "bbox": None},
        "account_number": {"text": "ACC1", "status": "read", "bbox": None},
        "holder": {"text": "H", "status": "read", "bbox": None},
        "period": {"text": "15.12.2024 - 14.01.2025", "status": "read", "bbox": None},
        "opening_balance": {"text": "1.000,00", "status": "read", "bbox": None},
        "closing_balance": {"text": "1.000,00", "status": "read", "bbox": None},
        "rows": [
            {
                "date_text": d,
                "description": "",
                "amount_text": a,
                "amount_column": "signed",
                "balance_text": "",
                "row_bbox": None,
                "amount_bbox": None,
                "balance_bbox": None,
            }
            for d, a in (("20.12.", "50,00"), ("03.01.", "-50,00"))
        ],
    }
    o = observe(reading, "x")
    assert [ln.day for ln in o.lines] == [date(2024, 12, 20), date(2025, 1, 3)]


def test_intraday_check_works_without_an_opening_balance():
    page2 = obs(
        "p2",
        date(2025, 3, 1),
        date(2025, 3, 31),
        None,
        D(5000),
        [(date(2025, 3, 5), D(-4999), D(0)), (date(2025, 3, 5), D(5000), D(5000))],
    )
    (r,) = reconcile([page2], SOLE, RATES, 2025)
    assert "possible_intraday_peak" in codes(r)


def test_usd_accounts_abroad_need_no_rate():
    (r,) = reconcile(
        [obs("usd", date(2025, 1, 1), date(2025, 1, 31), D("1500.10"), D("1500.10"), currency="USD")],
        SOLE,
        RATES,
        2025,
    )
    assert r.max_value_usd == 1501
    assert "currency_unknown" not in codes(r)


@pytest.mark.parametrize(
    "text, want",
    [
        ("All amounts in KRW", "KRW"),
        ("Currency: Korean Won", None),
        ("HK$", None),
        ("$", None),
        ("₩", "KRW"),
    ],
)
def test_currency_parsing_never_guesses(text, want):
    assert parse_currency(text) == want


def ledger_lines(rows):
    """Rows of a compiled multi-account list: (day, amount, account_text); no balances."""
    return [
        Line(i, d, a, None, "", Source("list", f"row:{i}", None), account_text=acct)
        for i, (d, a, acct) in enumerate(rows)
    ]


def compiled_page(rows):
    o = obs("list", date(2024, 9, 1), date(2026, 1, 31), None, None, key="")
    o.account_number, o.currency, o.lines = "", "KRW", ledger_lines(rows)
    return o


def test_a_list_covering_several_accounts_is_split_by_row():
    page = compiled_page(
        [
            (date(2025, 2, 3), D(5_000_000), "110-731-552904"),
            (date(2025, 3, 3), D(-1_000_000), "110-731-552904"),
            (date(2025, 1, 24), D(6_840_000), "110-48*-******"),
        ]
    )
    results = {
        r.account_key: r for r in reconcile([page], {"110731552904": Intake("sole", True)}, RATES, 2025)
    }
    assert set(results) == {"110731552904", "masked:11048*******"}
    mmda, masked = results["110731552904"], results["masked:11048*******"]
    assert mmda.max_value is None
    assert "no_balance_anchor" in codes(mmda)
    assert not masked.identified
    assert "account_number_masked" in codes(masked)
    assert fbar_totals(list(results.values())).aggregate_max_usd == 0


def test_won_unit_and_masked_keys():
    from cross_border_fbar.extract.parse import account_key

    assert parse_currency("금액 단위: 원") == "KRW"
    assert account_key("110-48*-******") == "masked:11048*******"
    assert account_key("110-731-552904") == "110731552904"


def test_a_statement_repeating_its_own_account_on_rows_keeps_its_balances():
    """Codex review on #4: repeating the header account per row used to discard opening and closing."""
    o = obs("s", date(2025, 1, 1), date(2025, 1, 31), D(1000), D(500), [(date(2025, 1, 9), D(-500), None)])
    o.lines[0].account_text = "ACC1"
    (r,) = reconcile([o], SOLE, RATES, 2025)
    assert r.max_value.amount == D(1000)
    assert "no_balance_anchor" not in codes(r)


def test_text_labelled_absent_is_still_used():
    """Glimmer sometimes returns the right header text with status 'absent' (Oct 28 run)."""
    reading = {
        n: {"text": t, "status": "absent", "bbox": None}
        for n, t in [
            ("institution", "Bank"),
            ("currency", "EUR"),
            ("account_number", "ACC1"),
            ("holder", "H"),
            ("period", "01.03.2025 - 31.03.2025"),
            ("opening_balance", "1.000,00"),
            ("closing_balance", "1.000,00"),
        ]
    }
    reading["rows"] = []
    o = observe(reading, "p")
    assert o.account_key == "ACC1"
    assert o.opening == D("1000.00")
    assert o.problems == []


def _named(observations, names):
    from dataclasses import replace

    return [replace(o, institution=n) for o, n in zip(observations, names, strict=True)]


def test_different_printed_bank_names_go_to_review_with_a_page_for_each():
    """Codex on #6, four rounds: every name-matching rule merged two banks or split one. Names are not
    matched; the CPA sees each printed name, its months and a page where it was printed."""
    names = ["Rheinauen Privatbank"] * 6 + ["Bergwald Bank AG"] * 6
    (r,) = reconcile(_named(year_of(), names), SOLE, RATES, 2025)
    (f,) = [f for f in r.flags if f.code == "institution_names_differ"]
    assert f.severity == "review"
    assert f.message == (
        "statements print different bank names: Rheinauen Privatbank (6 statements, 2025-01 to 2025-06); "
        "Bergwald Bank AG (6 statements, 2025-07 to 2025-12)"
    )
    assert [s.upload for s in f.sources] == ["ACC1-1", "ACC1-7"]
    assert [s.ref for s in f.sources] == ["institution", "institution"]
    assert r.institutions == ["Rheinauen Privatbank", "Bergwald Bank AG"]


@pytest.mark.parametrize(
    "a, b",
    [
        ("UBS", "UBS AG"),
        ("みずほ銀行", "りそな銀行"),
        ("ธนาคารสยามโลตัส", "Siam Lotus Bank"),
        ("Ngân hàng Việt Nam", "Ngân hàng Việt Á"),
    ],
)
def test_names_that_differ_in_any_way_are_flagged_not_guessed(a, b):
    (r,) = reconcile(_named(year_of(), [a] * 11 + [b]), SOLE, RATES, 2025)
    assert [f.code for f in r.flags if f.code == "institution_names_differ"] == ["institution_names_differ"]
    assert r.institutions == [a, b]


def test_spacing_and_case_alone_are_one_name():
    (r,) = reconcile(_named(year_of(), ["Siam  Lotus Bank", "siam lotus bank"] * 6), SOLE, RATES, 2025)
    assert "institution_names_differ" not in [f.code for f in r.flags]
    assert r.institutions == ["Siam  Lotus Bank"]


def test_unreadable_transaction_fields_preserve_their_source_rows():
    reading = {
        name: {"text": text, "status": "read", "bbox": None}
        for name, text in [
            ("institution", "Synthetic Bank"),
            ("currency", "USD"),
            ("account_number", "ACC1"),
            ("holder", "Synthetic Holder"),
            ("period", "01.01.2025 - 31.12.2025"),
            ("opening_balance", "1,000.00"),
            ("closing_balance", "1,000.00"),
        ]
    }
    reading["rows"] = [
        {
            "date_text": "unreadable",
            "description": "Synthetic unreadable transaction",
            "amount_text": "unreadable",
            "amount_column": "signed",
            "balance_text": "",
            "row_bbox": None,
            "amount_bbox": None,
            "balance_bbox": None,
        }
    ]
    page = observe(reading, "synthetic-unreadable")
    assert page.problems == ["row:0:date", "row:0:amount"]
    (account,) = reconcile([page], SOLE, RATES, 2025)
    review_items = [flag for flag in account.flags if flag.code == "field_unreadable"]
    assert len(review_items) == 2
    assert all(flag.sources[0].ref == "row:0" for flag in review_items)
    assert not account.annual_max_complete


def test_conflicting_boundary_readings_attach_every_source_and_keep_ordinary_intraday_changes():
    opening, closing = D(1000), D(1500)
    page = obs(
        "a",
        date(2025, 1, 1),
        date(2025, 12, 31),
        opening,
        closing,
        [(date(2025, 6, 1), D(200), D(1200)), (date(2025, 6, 1), D(300), D(1500))],
    )
    other = obs("b", date(2025, 12, 1), date(2025, 12, 31), closing, D(1400))
    (ordinary,) = reconcile([page], SOLE, RATES, 2025)
    assert "balance_readings_conflict" not in codes(ordinary)
    (disputed,) = reconcile([page, other], SOLE, RATES, 2025)
    (flag,) = [item for item in disputed.flags if item.code == "balance_readings_conflict"]
    assert [source.upload for source in flag.sources] == ["a", "b"]
    assert all(source.ref == "closing_balance" for source in flag.sources)
    assert disputed.year_end_balance is None


def test_lower_disputed_boundary_does_not_hide_an_independently_higher_annual_peak():
    page = obs(
        "year",
        date(2025, 1, 1),
        date(2025, 12, 31),
        D(1000),
        D(1000),
        [(date(2025, 1, 5), D(1000), D(2000)), (date(2025, 1, 6), D(-1000), D(1000))],
    )
    correction = obs("partial", date(2025, 1, 1), date(2025, 1, 31), D(900), D(1000))
    (result,) = reconcile([page, correction], SOLE, RATES, 2025)
    assert "balance_readings_conflict" in codes(result)
    assert result.max_value.amount == D(2000)
    assert result.annual_max_complete


def test_an_unknown_currency_cannot_leak_a_partial_maximum_into_fbar_totals():
    from cross_border_fbar.rates import Rate

    pages = [
        obs("known", date(2025, 1, 1), date(2025, 1, 31), D(10000), D(10000), currency="EUR"),
        obs(
            "unknown",
            date(2025, 12, 1),
            date(2025, 12, 31),
            D(100000),
            D(100000),
            currency=None,
            problems=["currency"],
        ),
    ]
    (account,) = reconcile(pages, SOLE, {"EUR": Rate("EUR", D(2), "2025-12-31", "synthetic rate")}, 2025)
    assert account.max_value is None
    assert account.max_value_usd is None
    assert account.maximum_bounds.low_usd == 5000
    assert account.maximum_bounds.high_usd is None
    totals = fbar_totals([account])
    assert totals.accounts_without_value == ["ACC1"]
    assert totals.accounts_counted == 0


def test_mixed_currencies_never_produce_a_native_dollar_or_year_end_value():
    from dataclasses import replace

    pages = year_of()
    pages[-1] = replace(pages[-1], currency="KRW", opening=D(1000000), closing=D(1000000))
    (r,) = reconcile(pages, SOLE, RATES, 2025)
    assert "currency_conflict" in codes(r)
    assert r.currency is r.max_value is r.max_value_usd is r.year_end_balance is None
    assert "continuity_break" not in codes(r)  # no arithmetic across unlike units
    (corrected,) = reconcile(pages, SOLE, RATES, 2025, currency_selections={"ACC1": "KRW"})
    assert corrected.currency == "KRW"
    assert corrected.max_value.amount == D(1000000)
    assert corrected.max_value_usd == 693
    assert corrected.year_end_balance == D(1000000)


def test_currency_is_part_of_duplicate_identity():
    from dataclasses import replace

    page = year_of()[0]
    (r,) = reconcile([page, replace(page, upload="other", currency="KRW")], SOLE, RATES, 2025)
    assert not r.duplicate_uploads
    assert "currency_conflict" in codes(r)
    assert r.max_value is None


@pytest.mark.parametrize(
    "interest,expected",
    [
        (False, "signature_authority_only"),
        (None, "financial_interest_unknown"),
        (True, None),
    ],
)
def test_signature_authority_checks_financial_interest(interest, expected):
    (r,) = reconcile(year_of(), {"ACC1": Intake("signature_authority", interest)}, RATES, 2025)
    relevant = codes(r) & {"signature_authority_only", "financial_interest_unknown"}
    assert relevant == ({expected} if expected else set())


def test_identified_account_with_no_usable_data_has_a_review_flag():
    empty = obs("empty", date(2025, 1, 1), date(2025, 12, 31), None, None)
    (r,) = reconcile([empty], SOLE, RATES, 2025)
    assert "no_usable_balance" in codes(r)
    assert r.max_value is None


def test_out_of_year_only_accounts_retain_identity_and_uploads():
    page = obs("old", date(2024, 1, 1), date(2024, 12, 31), D(1), D(2))
    (r,) = reconcile([page], SOLE, RATES, 2025)
    assert r.account_key == r.account_number == "ACC1"
    assert r.uploads == ["old"]
    assert "no_usable_balance" in codes(r)
    flag = next(item for item in r.flags if item.code == "no_usable_balance")
    assert [source.upload for source in flag.sources] == ["old"]


def test_currency_conflicts_keep_the_currency_source_from_every_statement():
    pages = [
        obs("eur", date(2025, 1, 1), date(2025, 1, 31), D(1000), D(1000), currency="EUR"),
        obs("krw", date(2025, 2, 1), date(2025, 2, 28), D(1000), D(1000), currency="KRW"),
    ]
    (account,) = reconcile(pages, SOLE, RATES, 2025)
    (flag,) = [item for item in account.flags if item.code == "currency_conflict"]
    assert [source.upload for source in flag.sources] == ["eur", "krw"]
    assert all(source.ref == "currency" for source in flag.sources)


def test_an_empty_currency_selection_does_not_release_conflicting_currency_values():
    from cross_border_fbar.reconcile.accounts import reconcile_account

    pages = [
        obs("eur", date(2025, 1, 1), date(2025, 1, 31), D(1000), D(1000), currency="EUR"),
        obs("krw", date(2025, 2, 1), date(2025, 2, 28), D(1000), D(1000), currency="KRW"),
    ]
    account = reconcile_account(pages, SOLE["ACC1"], RATES["EUR"], 2025, 1000, currency_selection="")
    assert account.max_value is None
    assert account.max_value_usd is None
    assert account.year_end_balance is None
