"""The review app: worksheet build from recorded readings, review rules, scope guard, server routes."""

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cross_border_fbar.app import phrases as phrase_tables
from cross_border_fbar.app.review import CLIENT_LABELS, SUMMARY_PHRASES, Review, ReviewError
from cross_border_fbar.app.server import make_handler
from cross_border_fbar.app.worksheet import Inputs, build, worksheet_hash

REPO = Path(__file__).resolve().parents[1]
READINGS = REPO / "results" / "muse-spark-1.3_synth.json"


@pytest.fixture
def review(inputs, tmp_path) -> Review:
    return Review(inputs, tmp_path / "review", root=tmp_path)


def test_worksheet_traces_every_figure_and_never_reads_the_answer_key(inputs, monkeypatch):
    real_read = Path.read_text

    def guarded(self, *a, **k):
        assert self.name != "truth.json", "the worksheet must not read the answer key"
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", guarded)
    ws = build(inputs)
    valued = [a for a in ws["accounts"] if a["max"]]
    assert len(valued) == 4
    assert all(a["max"]["source"]["bbox"] and a["max"]["source"]["wp"] for a in valued)
    assert ws["totals"]["aggregate_usd"] == 373_618
    assert worksheet_hash(ws) == worksheet_hash(build(inputs))


def test_sign_off_needs_every_review_item_and_then_freezes(review):
    with pytest.raises(ReviewError, match="still need"):
        review.sign_off("A. Rivera", "AR")
    with pytest.raises(ReviewError, match="note"):
        review.acknowledge(review.open_review_flags()[0], "  ", "A. Rivera")
    open_items = review.open_review_flags()
    for fid in open_items:
        review.acknowledge(fid, "checked", "A. Rivera")
    review.sign_off("A. Rivera", "AR")
    assert review.state["signoff"]["hash"] == worksheet_hash(review.worksheet)
    with pytest.raises(ReviewError, match="signed off"):
        review.set_ownership(review.worksheet["accounts"][0]["key"], "joint", "late", "A. Rivera")
    actions = [e["action"] for e in review.audit_tail(100)]
    assert actions.count("acknowledge") == len(open_items) > 0
    assert actions[-1] == "sign_off"


def test_ownership_correction_preserves_financial_interest_and_shows_review(review):
    acct = next(a for a in review.worksheet["accounts"] if a["ownership"] == "sole" and a["max"])
    review.set_ownership(acct["key"], "signature_authority", "client clarified by email", "A. Rivera")
    row = next(a for a in review.worksheet["form8938"]["accounts"] if a["key"] == acct["key"])
    codes = {f["code"] for f in review.worksheet["flags"] if f["account"] == acct["key"]}
    assert "signature_authority_only" not in codes
    assert row["financial_interest"] is True
    assert row["inclusion"] == "included"
    assert any("signature authority and financial interest" in note for note in row["reviews"])
    assert review.audit_tail(1)[0]["after"] == "signature_authority"


@pytest.fixture
def signed(review, tmp_path, monkeypatch):
    """A signed review with no reviewed phrase table, so every phrase goes to the translator."""
    monkeypatch.setattr(phrase_tables, "PHRASES_DIR", tmp_path / "no-tables")
    for fid in review.open_review_flags():
        review.acknowledge(fid, "INTERNAL-NOTE-7731", "A. Rivera")
    review.sign_off("A. Rivera", "AR")
    return review


def tokens(phrases, language, fresh):
    return {k: f"«{k}»" for k in phrases}, {"model": "fake", "fresh": fresh}


def test_summary_is_assembled_only_from_fixed_phrases_and_signed_figures(review, signed):
    with pytest.raises(ReviewError, match="Sign off"):
        Review(review.inputs, review.dir.parent / "other", root=review.dir.parent).draft_summary(tokens)
    seen = {}

    def spy(phrases, language, fresh):
        seen.update(phrases)
        return tokens(phrases, language, fresh)

    s = signed.draft_summary(spy)
    assert set(seen) - set(CLIENT_LABELS) == set(SUMMARY_PHRASES)
    assert all(seen[k] == {**SUMMARY_PHRASES, **CLIENT_LABELS}[k] for k in seen)  # English from the tables
    assert "INTERNAL-NOTE-7731" not in json.dumps(seen)  # the preparer's notes stay internal
    lines = s["text"].splitlines()
    assert lines[0] == "«heading»"
    assert "새별은행 · «account_ending» 3746 · «highest_balance» 287,450,200 KRW (199,100.00 USD)" in lines
    assert "  «reviewed»: «nothing_further»" in lines
    # Every word outside the figures and bank names is a fixed phrase.
    words = set(re.findall(r"[A-Za-z가-힣]+", re.sub(r"«[a-z_]+»", "", s["text"])))
    banks = {
        w
        for a in signed.worksheet["accounts"]
        for n in a["institutions"]
        for w in re.findall(r"[A-Za-z가-힣]+", n)
    }
    assert words - banks <= {"KRW", "EUR", "THB", "USD"}
    assert set(s["unreviewed"]) == set(seen)


@pytest.mark.parametrize(
    "bad",
    [
        lambda p: {**{k: "x" for k in p}, "extra": "y"},
        lambda p: {k: "x" for k in list(p)[1:]},
        lambda p: {**{k: "x" for k in p}, "heading": "2026년 4월 15일까지"},
        lambda p: {**{k: "x" for k in p}, "heading": "a\nb"},
        lambda p: {**{k: "x" for k in p}, "reviewed": "신고하셔야 합니다"},
        lambda p: {**{k: "x" for k in p}, "reviewed": "x" * 200},
        lambda p: None,
        lambda p: ["not an object"],
        lambda p: "not an object",
        lambda p: 17,
        lambda p: True,
    ],
)
def test_a_translation_that_adds_anything_is_held_back(signed, bad):
    s = signed.draft_summary(lambda phrases, language, fresh: (bad(phrases), {"model": "fake"}))
    assert s["blocked"]
    assert s["text"] is None
    with pytest.raises(ReviewError, match="can't be released"):
        signed.approve_summary("A. Rivera", confirm_unreviewed=True)


def test_unreviewed_phrases_are_approved_only_when_confirmed(signed):
    """'you should file an FBAR' passed the word check. A phrase no one has reviewed is shown
    to the preparer and needs explicit confirmation; the reviewed table is the normal path."""

    def stray(phrases, language, fresh):
        out, meta = tokens(phrases, language, fresh)
        return {**out, "reviewed": "you should file an FBAR"}, meta

    s = signed.draft_summary(stray)
    assert s["unreviewed"]["reviewed"] == "you should file an FBAR"
    with pytest.raises(ReviewError, match="haven't been reviewed"):
        signed.approve_summary("A. Rivera")
    s = signed.draft_summary(tokens)
    assert s["meta"]["fresh"] is True  # a redraft never replays the held draft
    signed.approve_summary("A. Rivera", confirm_unreviewed=True)
    assert signed.audit_tail(1)[0]["confirmed_unreviewed"] == sorted(s["unreviewed"])


def test_a_reviewed_table_needs_no_model_call_and_stale_entries_are_retranslated(
    signed, tmp_path, monkeypatch
):
    from babel.messages.catalog import Catalog

    d = tmp_path / "tables"
    d.mkdir()
    monkeypatch.setattr(phrase_tables, "PHRASES_DIR", d)
    catalog = Catalog(locale="ko", last_translator="A. Reviewer", fuzzy=False)
    for key, en in {**SUMMARY_PHRASES, **CLIENT_LABELS}.items():
        catalog.add(en, f"[{key}]", context=key)
    phrase_tables.save("ko", catalog)

    def no_call(*a, **k):
        raise AssertionError("a reviewed table needs no translation")

    s = signed.draft_summary(no_call)
    assert s["unreviewed"] == {}
    assert s["text"].startswith("[heading]")
    signed.approve_summary("A. Rivera")
    catalog.delete(SUMMARY_PHRASES["heading"], context="heading")
    catalog.add("an older English heading", "[heading]", context="heading")
    phrase_tables.save("ko", catalog)
    s = signed.draft_summary(tokens)
    assert list(s["unreviewed"]) == ["heading"]


def test_first_summary_uses_the_committed_catalog_without_model_or_key(review, monkeypatch):
    import socket

    from cross_border_fbar import llm

    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_api_key", lambda: pytest.fail("API key was accessed"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network was used"))
    for flag_id in review.open_review_flags():
        review.acknowledge(flag_id, "reviewed synthetic evidence", "Preparer")
    review.sign_off("Preparer", "PR")
    summary = review.draft_summary(lambda *a, **k: pytest.fail("model was called"))
    # The catalog's one unreviewed entry is a draft: it needs the preparer's confirmation, not a model call.
    assert summary["unreviewed"] == {"heading": "세무 대리인이 검토한 항목"}
    assert summary["text"]
    with pytest.raises(ReviewError, match="haven't been reviewed"):
        review.approve_summary("Preparer")
    review.approve_summary("Preparer", confirm_unreviewed=True)


def test_the_committed_korean_table_covers_every_phrase_with_current_english():
    """Changing an English phrase or adding a review flag makes the table stale; this fails until it is
    translated and reviewed again."""
    catalog = phrase_tables.load("ko")
    current = {**SUMMARY_PHRASES, **CLIENT_LABELS}
    assert all(catalog.get(en, context=k) is not None for k, en in current.items())


def test_server_serves_pages_but_not_files_outside_them(inputs, tmp_path):
    srv = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(Review(inputs, tmp_path / "r", root=tmp_path), inputs.set_dir)
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = next((inputs.set_dir / "pages").glob("*.png")).name
        assert urllib.request.urlopen(f"{base}/doc/pages/{page}").status == 200
        assert json.loads(urllib.request.urlopen(f"{base}/api/state").read())["worksheet"]["accounts"]
        for bad in ("/doc/pages/../truth.json", "/doc/pages/%2e%2e/truth.json", "/static/../review.py"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(base + bad)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/api/export")
        assert e.value.code == 409
    finally:
        srv.shutdown()


def test_a_sign_off_is_invalidated_when_the_figures_change(inputs, tmp_path):
    """A restart with changed readings or intake kept the old sign-off."""
    first = Review(inputs, tmp_path / "rv", root=tmp_path)
    for fid in first.open_review_flags():
        first.acknowledge(fid, "checked", "A. Rivera")
    first.sign_off("A. Rivera", "AR")
    intake = json.loads(inputs.intake.read_text(encoding="utf-8"))
    intake["accounts"][0]["ownership"] = "joint"  # the figures that were signed no longer match
    changed = tmp_path / "intake-changed.json"
    changed.write_text(json.dumps(intake), encoding="utf-8")
    again = Review(Inputs(inputs.set_dir, inputs.readings, changed), tmp_path / "rv", root=tmp_path)
    assert again.state["signoff"] is None
    assert again.state["invalidated_signoffs"][0]["initials"] == "AR"
    assert again.audit_tail(1)[0]["action"] == "signoff_invalidated"
    with pytest.raises(ReviewError):
        again.export()


@pytest.mark.parametrize(
    "text",
    [
        "신고하셔야 합니다",
        "신고 대상입니다",
        "서류를 제출하셔야 합니다",
        "가산세가 부과될 수 있습니다",
        "You must file an FBAR.",
        "Penalties may apply.",
    ],
)
def test_scope_guard_catches_obligation_wording_in_any_inflection(text):
    """Polite Korean inflections slipped past the literal patterns."""
    from cross_border_fbar.app.review import OUT_OF_SCOPE

    assert OUT_OF_SCOPE.search(text)


def test_review_folder_must_stay_inside_the_working_directory(inputs, tmp_path):
    with pytest.raises(ValueError):
        Review(inputs, tmp_path / ".." / "elsewhere", root=tmp_path)


@pytest.mark.parametrize("statement_id", ["../truth", "..", "a/b", "/etc/passwd", "x\x00y"])
def test_a_crafted_readings_file_cannot_reach_outside_the_pages_folder(inputs, statement_id):
    from cross_border_fbar.app.worksheet import page_image

    with pytest.raises(ValueError):
        page_image(inputs.set_dir, statement_id, 1)


def test_two_coverage_gaps_on_one_account_need_separate_acknowledgements():
    """Same-code flags without month or sources shared one ID."""
    from cross_border_fbar.app.worksheet import flag_id
    from cross_border_fbar.reconcile.accounts import Flag

    a = Flag("coverage_gap", "no statement covers 2025-01-01 to 2025-01-14")
    b = Flag("coverage_gap", "no statement covers 2025-12-16 to 2025-12-31")
    assert flag_id("ACC1", a) != flag_id("ACC1", b)


def test_every_flag_emitted_anywhere_is_in_the_catalog_and_no_catalog_entry_is_dead():
    import ast

    from cross_border_fbar.reconcile.flags import CATALOG

    pkg = Path(__file__).resolve().parents[1] / "src" / "cross_border_fbar"
    raised = set()
    for path in pkg.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Flag":
                assert isinstance(node.args[0], ast.Constant), f"dynamic flag code in {path}"
                raised.add(node.args[0].value)
    assert raised == set(CATALOG)
    assert CLIENT_LABELS == {
        k: v.client_phrase for k, v in CATALOG.items() if v.severity == "review" and not v.preparer_only
    }
    for definition in CATALOG.values():
        assert ";" not in definition.client_phrase
        assert "the client" not in definition.client_phrase
        assert all(
            (
                definition.severity,
                definition.condition,
                definition.cpa_action,
                definition.title,
                definition.client_phrase or definition.preparer_only,
            )
        )


def test_failed_pages_and_accounts_without_statements_block_sign_off(inputs, tmp_path):
    """Pages whose extraction failed were dropped, so a readings file of failures signed off
    as an empty $0 worksheet. Each failed page and each intake account with nothing read is a review item."""
    readings = json.loads(READINGS.read_text(encoding="utf-8"))
    failed = {"statement_id": "kr-checking_2025-03", "page": 1, "error": "HTTP 503 from the model API"}
    readings["pages"] = [failed] + [
        p for p in readings["pages"] if not p["statement_id"].startswith("kr-checking")
    ]
    path = tmp_path / "readings.json"
    path.write_text(json.dumps(readings), encoding="utf-8")
    ws = build(Inputs(inputs.set_dir, path, inputs.intake))
    codes = {(f["code"], f["account"]) for f in ws["flags"]}
    assert ("page_not_read", "unread:pages/kr-checking_2025-03-p1.png") in codes
    (page_flag,) = [f for f in ws["flags"] if f["code"] == "page_not_read"]
    assert page_flag["severity"] == "review"
    assert "HTTP 503" in page_flag["message"]
    assert page_flag["sources"][0]["upload"] == "pages/kr-checking_2025-03-p1.png"
    (kr,) = [a for a in ws["accounts"] if a["account_number"] == "110-482-913746"]
    assert kr["max"] is None
    assert kr["identified"]
    assert [f["code"] for f in ws["flags"] if f["account"] == kr["key"]] == ["no_statements"]
    assert "110-482-913746" in ws["totals"]["accounts_without_value"]
    assert "pages/kr-checking_2025-03-p1.png" in ws["totals"]["unidentified_uploads"]

    review = Review(Inputs(inputs.set_dir, path, inputs.intake), tmp_path / "review", root=tmp_path)
    with pytest.raises(ReviewError, match="still need"):
        review.sign_off("A. Rivera", "AR")


def test_only_failed_pages_still_show_every_intake_account(inputs, tmp_path):
    readings = json.loads(READINGS.read_text(encoding="utf-8"))
    readings["pages"] = [
        {"statement_id": p["statement_id"], "page": p.get("page", 1), "error": "timeout"}
        for p in readings["pages"]
    ]
    path = tmp_path / "readings.json"
    path.write_text(json.dumps(readings), encoding="utf-8")
    ws = build(Inputs(inputs.set_dir, path, inputs.intake))
    intake_numbers = {
        a["account_number"] for a in json.loads(inputs.intake.read_text(encoding="utf-8"))["accounts"]
    }
    assert {a["account_number"] for a in ws["accounts"] if a["identified"]} == intake_numbers
    assert sum(f["code"] == "page_not_read" for f in ws["flags"]) == len(readings["pages"])
    assert ws["totals"]["aggregate_usd"] == 0


def test_every_worksheet_field_is_in_the_signed_fingerprint(inputs):
    """The fingerprint listed its fields, so the client's name could change under a sign-off.
    Changing any top-level field must change the fingerprint."""
    ws = build(inputs)
    for key in ws:
        changed = {**ws, key: {"changed": key}}
        assert worksheet_hash(changed) != worksheet_hash(ws), key


def test_a_sign_off_is_invalidated_when_the_client_changes(inputs, tmp_path):
    first = Review(inputs, tmp_path / "rv", root=tmp_path)
    for fid in first.open_review_flags():
        first.acknowledge(fid, "checked", "A. Rivera")
    first.sign_off("A. Rivera", "AR")
    intake = json.loads(inputs.intake.read_text(encoding="utf-8"))
    intake["client"] = "Another Client"
    changed = tmp_path / "intake-client.json"
    changed.write_text(json.dumps(intake), encoding="utf-8")
    again = Review(Inputs(inputs.set_dir, inputs.readings, changed), tmp_path / "rv", root=tmp_path)
    assert again.state["signoff"] is None
    assert again.state["summary"] is None
    with pytest.raises(ReviewError):
        again.export()


@pytest.mark.parametrize("language", ["../../outside", "/tmp/x", "ko/../..", "en"])
def test_a_phrase_table_path_takes_only_a_listed_language(language):
    """The language from the command line reached a file path."""
    with pytest.raises(ValueError, match="no client language"):
        phrase_tables.table_path(language)


def test_only_this_apps_own_page_can_act_or_read(inputs, tmp_path):
    """Another site open in the preparer's browser could POST a sign-off to localhost, and a
    rebound DNS name could read the state. Requests must name this app's host; POSTs must be JSON from
    this origin."""
    review = Review(inputs, tmp_path / "r", root=tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(review, inputs.set_dir))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def status(path, body=None, headers=()):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, headers=dict(headers))
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code

    fid = review.open_review_flags()[0]
    ack = {"flag": fid, "note": "checked", "reviewer": "A. Rivera"}
    json_ct = ("Content-Type", "application/json")
    try:
        assert status("/api/state", headers=[("Host", "evil.example:80")]) == 403
        assert status("/api/ack", ack, [json_ct, ("Origin", "https://evil.example")]) == 403
        assert status("/api/ack", ack, [("Content-Type", "text/plain")]) == 403  # a form or simple request
        assert fid in review.open_review_flags()
        assert status("/api/ack", ack, [json_ct, ("Origin", base)]) == 200
        assert fid not in review.open_review_flags()
    finally:
        srv.shutdown()


def test_every_row_and_review_item_has_its_own_id(inputs, tmp_path):
    """Rows without an account number shared an empty key, so two of them missing the same
    month got one review ID and one acknowledgement covered both."""
    readings = json.loads(READINGS.read_text(encoding="utf-8"))
    blanked = 0
    for page in readings["pages"]:
        if page["statement_id"].startswith("th-savings") and "reading" in page and blanked < 2:
            page["reading"]["account_number"] = {
                **page["reading"]["account_number"],
                "text": "",
                "status": "unreadable",
            }
            blanked += 1
    readings["pages"].append({"statement_id": "de-joint_2025-01", "page": 1, "error": "timeout"})
    path = tmp_path / "readings.json"
    path.write_text(json.dumps(readings), encoding="utf-8")
    for ws in (build(inputs), build(Inputs(inputs.set_dir, path, inputs.intake))):
        keys = [a["key"] for a in ws["accounts"]]
        ids = [f["id"] for f in ws["flags"]]
        assert len(keys) == len(set(keys))
        assert len(ids) == len(set(ids))
    assert sum(1 for a in ws["accounts"] if a["key"].startswith("unidentified:")) == 2


def test_the_eval_does_not_count_intake_rows_as_found(inputs, tmp_path, monkeypatch):
    """A row for every intake account made accounts_found perfect even with no page read."""
    from cross_border_fbar.eval import reconcile_eval

    readings = json.loads(READINGS.read_text(encoding="utf-8"))
    readings["pages"] = [
        {"statement_id": p["statement_id"], "page": p.get("page", 1), "error": "x"} for p in readings["pages"]
    ]
    results = tmp_path / "results"
    results.mkdir()
    (results / f"muse-spark-1.3_{inputs.set_dir.name}.json").write_text(
        json.dumps(readings), encoding="utf-8"
    )
    monkeypatch.setattr(reconcile_eval, "RESULTS", results)
    out = reconcile_eval.evaluate(inputs.set_dir, "muse-spark-1.3")
    assert out["summary"]["accounts_found"].startswith("0/")


def altered_review(inputs, tmp_path, mutate):
    doc = json.loads(inputs.readings.read_text(encoding="utf-8"))
    mutate(doc)
    readings = tmp_path / "altered-readings.json"
    readings.write_text(json.dumps(doc), encoding="utf-8")
    return Review(Inputs(inputs.set_dir, readings, inputs.intake), tmp_path / "changed-review", root=tmp_path)


def test_currency_selection_is_audited_and_required_before_acknowledgement(inputs, tmp_path):
    def mutate(doc):
        for page in doc["pages"]:
            if page["statement_id"] == "kr-checking_2025-12" and "reading" in page:
                page["reading"]["currency"]["text"] = "EUR"

    review = altered_review(inputs, tmp_path, mutate)
    flag = next(f for f in review.worksheet["flags"] if f["code"] == "currency_conflict")
    before_hash = worksheet_hash(review.worksheet)
    account = next(a for a in review.worksheet["accounts"] if a["key"] == flag["account"])
    assert account["max"] is account["max_usd"] is account["year_end_balance"] is None
    with pytest.raises(ReviewError, match="Select the account currency"):
        review.acknowledge(flag["id"], "checked", "Preparer")
    with pytest.raises(ReviewError, match="source note"):
        review.set_currency(account["key"], "KRW", "", "Preparer")
    review.set_currency(account["key"], "KRW", "Synthetic bank confirms currency label error", "Preparer")
    account = next(a for a in review.worksheet["accounts"] if a["key"] == flag["account"])
    assert account["currency"] == "KRW"
    assert account["max_usd"] == 199100
    assert account["currency_correction"]["reviewer"] == "Preparer"
    assert review.audit_tail(1)[0]["action"] == "correct_currency"
    assert worksheet_hash(review.worksheet) != before_hash
    review.acknowledge(flag["id"], "currency resolved", "Preparer")
    again = Review(review.inputs, review.dir, root=tmp_path)
    assert again.worksheet == review.worksheet
    assert flag["id"] not in again.open_review_flags()


def review_without_treasury_rate(inputs, tmp_path, extra_review=False):
    def mutate(doc):
        for page in doc["pages"]:
            if page["statement_id"].startswith("th-savings") and "reading" in page:
                page["reading"]["currency"]["text"] = "INR"
                if extra_review and page["statement_id"] == "th-savings_2025-01":
                    page["reading"]["institution"]["text"] = "Synthetic Alternative Bank Name"

    review = altered_review(inputs, tmp_path, mutate)
    flag = next(f for f in review.worksheet["flags"] if f["code"] == "currency_unknown")
    return review, flag


def test_alternative_rate_acknowledgement_recomputes_persists_and_reopens(inputs, tmp_path):
    from decimal import ROUND_CEILING, Decimal

    review, flag = review_without_treasury_rate(inputs, tmp_path)
    account = next(a for a in review.worksheet["accounts"] if a["key"] == flag["account"])
    assert account["max_usd"] is None
    rate = {"units_per_usd": "2.5", "source": "Synthetic alternative rate table", "record_date": "2025-12-31"}
    review.acknowledge(flag["id"], "verified year-end rate", "Preparer", rate=rate)
    account = next(a for a in review.worksheet["accounts"] if a["key"] == flag["account"])
    assert account["max_usd"] == int(
        (Decimal(account["max"]["amount"]) / Decimal("2.5")).to_integral_value(rounding=ROUND_CEILING)
    )
    assert account["rate_source"] == rate["source"]
    assert account["rate_date"] == "2025-12-31"
    assert account["rate_basis"] == "cpa"
    assert review.audit_tail(2)[0]["rate"]["units_per_usd"] == "2.5"
    again = Review(review.inputs, review.dir, root=tmp_path)
    assert again.worksheet == review.worksheet
    assert flag["id"] not in again.open_review_flags()
    again.reopen(flag["id"], "Preparer")
    account = next(a for a in again.worksheet["accounts"] if a["key"] == flag["account"])
    assert account["max_usd"] is None
    assert flag["id"] in again.open_review_flags()


@pytest.mark.parametrize(
    "change",
    [
        {"units_per_usd": "0"},
        {"units_per_usd": "-2"},
        {"units_per_usd": "NaN"},
        {"units_per_usd": "Infinity"},
        {"units_per_usd": "oops"},
        {"source": " "},
        {"source": None},
        {"source": []},
        {"record_date": "2025-06-30"},
        {"record_date": "bad-date"},
    ],
)
def test_alternative_rates_require_a_positive_rate_verifiable_source_and_year_end_date(
    inputs, tmp_path, change
):
    review, flag = review_without_treasury_rate(inputs, tmp_path)
    rate = {"units_per_usd": "2.5", "source": "Synthetic rate table", "record_date": "2025-12-31", **change}
    with pytest.raises(ReviewError):
        review.acknowledge(flag["id"], "checked", "Preparer", rate=rate)
    assert not review.state["rates"]
    assert flag["id"] in review.open_review_flags()


def test_masked_and_unread_items_reach_the_client_note(inputs, tmp_path, monkeypatch):
    def mutate(doc):
        doc["pages"].append({"statement_id": "de-joint_2025-01", "page": 1, "error": "timeout"})
        for page in doc["pages"]:
            if page["statement_id"] == "th-savings_2025-01" and "reading" in page:
                page["reading"]["account_number"]["text"] = "123-***-789"

    review = altered_review(inputs, tmp_path, mutate)
    monkeypatch.setattr(phrase_tables, "PHRASES_DIR", tmp_path / "missing")
    for fid in review.open_review_flags():
        review.acknowledge(fid, "checked", "Preparer")
    review.sign_off("Preparer", "PR")
    facts = review.summary_facts()
    assert set(facts["excluded_items"]) == {"account_number_masked", "page_not_read"}
    draft = review.draft_summary(tokens)
    assert "«excluded_items»: «account_number_masked»; «page_not_read»" in draft["text"]


def test_stale_and_fuzzy_gettext_entries_are_unreviewed(tmp_path, monkeypatch):
    from babel.messages.catalog import Catalog

    monkeypatch.setattr(phrase_tables, "PHRASES_DIR", tmp_path)
    catalog = Catalog(locale="ko", last_translator="Human Reviewer", fuzzy=False)
    catalog.add("old label", "오래된 문구", context="label")
    catalog.add("fuzzy label", "검토할 문구", context="fuzzy", flags={"fuzzy"})
    phrase_tables.save("ko", catalog)
    requested = {"label": "new label", "fuzzy": "fuzzy label"}
    assert phrase_tables.reviewed("ko", requested) == {}
    updated = phrase_tables.sync("ko", requested)
    assert updated.get("new label", context="label").fuzzy
    assert updated.get("new label", context="label").previous_id == ["old label"]
    phrase_tables.save("ko", updated)
    assert phrase_tables.load("ko").get("new label", context="label").fuzzy


def test_catalog_supplies_worksheet_titles_actions_and_severity(inputs):
    from cross_border_fbar.reconcile.flags import CATALOG

    for flag in build(inputs)["flags"]:
        definition = CATALOG[flag["code"]]
        assert flag["title"] == definition.title
        assert flag["cpa_action"] == definition.cpa_action
        assert flag["severity"] == definition.severity


def test_numeric_client_formatting_keeps_decimal_precision_and_currency_rules():
    from cross_border_fbar.app.review import _money

    assert _money("9007199254740993.51", "EUR") == "9,007,199,254,740,993.51 EUR"
    assert _money("1234", "KRW", "ko") == "1,234 KRW"
    assert _money(None, "EUR") is None


def test_unreviewed_catalog_drafts_are_reused_but_need_explicit_approval(signed, tmp_path, monkeypatch):
    from babel.messages.catalog import Catalog

    monkeypatch.setattr(phrase_tables, "PHRASES_DIR", tmp_path)
    catalog = Catalog(locale="ko", last_translator="", fuzzy=False)
    for key, english in {**SUMMARY_PHRASES, **CLIENT_LABELS}.items():
        catalog.add(english, f"[{key}]", context=key, flags={"fuzzy"})
    phrase_tables.save("ko", catalog)

    def no_call(*args, **kwargs):
        raise AssertionError("current catalog drafts need no extra translation call")

    summary = signed.draft_summary(no_call)
    assert summary["unreviewed"]
    assert summary["text"].startswith("[heading]")
    with pytest.raises(ReviewError, match="haven't been reviewed"):
        signed.approve_summary("Preparer")
    signed.approve_summary("Preparer", confirm_unreviewed=True)


def test_updated_figures_reopen_previously_acknowledged_account_flags(inputs, tmp_path):
    review, flag = review_without_treasury_rate(inputs, tmp_path, extra_review=True)
    account_flags = [f for f in review.worksheet["flags"] if f["account"] == flag["account"]]
    for item in account_flags:
        review.acknowledge(item["id"], "checked", "Preparer")
    review.acknowledge(
        flag["id"],
        "rate verified",
        "Preparer",
        rate={
            "units_per_usd": "2",
            "source": "Synthetic alternative source",
            "record_date": "2025-12-31",
        },
    )
    other_reviews = [
        f["id"] for f in account_flags if f["code"] != "currency_unknown" and f["severity"] == "review"
    ]
    assert other_reviews
    assert set(other_reviews) <= set(review.open_review_flags())
    assert flag["id"] not in review.open_review_flags()


def test_printout_uses_the_fixed_project_document_and_rejects_path_arguments(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(phrase_tables, "printout", lambda language: "English | Korean")
    target = phrase_tables.write_printout("ko", root=tmp_path)
    assert target == tmp_path / "docs" / "korean-review.txt"
    assert target.read_text(encoding="utf-8") == "English | Korean"
    monkeypatch.setattr(sys, "argv", ["phrases", "--printout", "../../outside.txt"])
    with pytest.raises(SystemExit) as error:
        phrase_tables.main()
    assert error.value.code == 2


@pytest.mark.parametrize("link_kind", ["directory", "file"])
def test_printout_cannot_follow_symlinks_outside_the_project(tmp_path, link_kind):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    docs = workspace / "docs"
    if link_kind == "directory":
        docs.symlink_to(outside, target_is_directory=True)
    else:
        docs.mkdir()
        (docs / "korean-review.txt").symlink_to(outside / "overwritten.txt")
    with pytest.raises(ValueError, match="outside"):
        phrase_tables.write_printout("ko", root=workspace)
    assert list(outside.iterdir()) == []


def test_catalog_sync_removes_multiple_obsolete_entries_without_mutating_its_iterator(tmp_path, monkeypatch):
    from babel.messages.catalog import Catalog

    monkeypatch.setattr(phrase_tables, "PHRASES_DIR", tmp_path)
    catalog = Catalog(locale="ko", last_translator="", fuzzy=False)
    catalog.add("old first", "첫째", context="first")
    catalog.add("old second", "둘째", context="second")
    phrase_tables.save("ko", catalog)
    updated = phrase_tables.sync("ko", {"current": "current label"})
    assert [(message.context, message.id) for message in updated if message.id] == [
        ("current", "current label")
    ]


def test_client_changes_reopen_acknowledgements_even_when_account_figures_stay_the_same(inputs, tmp_path):
    review = Review(inputs, tmp_path / "client-review", root=tmp_path)
    flag_id = review.open_review_flags()[0]
    review.acknowledge(flag_id, "reviewed for the original client", "Preparer")
    intake = json.loads(inputs.intake.read_text(encoding="utf-8"))
    intake["client"] = "Another Synthetic Client"
    changed = tmp_path / "new-client.json"
    changed.write_text(json.dumps(intake), encoding="utf-8")
    reopened = Review(Inputs(inputs.set_dir, inputs.readings, changed), review.dir, root=tmp_path)
    assert flag_id in reopened.open_review_flags()
    assert flag_id not in reopened.view()["review"]["acks"]


def test_catalog_instruction_changes_reopen_the_corresponding_acknowledgements(review):
    flag_id = review.open_review_flags()[0]
    review.acknowledge(flag_id, "reviewed under the previous instruction", "Preparer")
    flag = next(item for item in review.worksheet["flags"] if item["id"] == flag_id)
    flag["cpa_action"] = "A different source check is now required."
    assert flag_id in review.open_review_flags()


def test_export_contains_current_acknowledgements_and_preserves_history_in_the_audit(review):
    flag = next(item for item in review.worksheet["flags"] if item["code"] == "signature_authority_only")
    review.acknowledge(flag["id"], "reviewed previous classification", "Preparer")
    review.set_ownership(flag["account"], "sole", "updated synthetic intake", "Preparer")
    for flag_id in review.open_review_flags():
        review.acknowledge(flag_id, "reviewed current worksheet", "Preparer")
    review.sign_off("Preparer", "PR")
    exported = review.export()
    assert flag["id"] not in exported["acknowledgements"]
    assert any(entry.get("flag") == flag["id"] for entry in exported["audit"])


def test_acknowledged_limitation_remains_visible_after_signoff_and_in_export(review):
    limitation = next(f for f in review.worksheet["flags"] if f["code"] == "missing_statement")
    assert review.view()["remaining_review_items"] == []
    for flag_id in review.open_review_flags():
        review.acknowledge(flag_id, "limitation acknowledged", "Preparer")
    review.sign_off("Preparer", "PR")
    visible = review.view()["remaining_review_items"]
    document = review.export()
    exported = document["remaining_review_items"]
    assert visible == exported
    assert {item["id"] for item in exported} == {
        flag["id"] for flag in review.worksheet["flags"] if flag["severity"] == "review"
    }
    assert document["figure_labels"]["max"] == "highest observed balance"
    item = next(f for f in exported if f["id"] == limitation["id"])
    assert item["code"] == "missing_statement"
    assert item["message"] == limitation["message"]
    assert item["acknowledgement"]["note"] == "limitation acknowledged"
    restarted = Review(review.inputs, review.dir, root=review.dir.parent)
    assert restarted.view()["remaining_review_items"] == exported
    assert restarted.export()["remaining_review_items"] == exported
    determinations = document["open_determinations"]
    assert determinations == review.view()["open_determinations"] == restarted.export()["open_determinations"]
    assert any(d["kind"] == "form8938_year_end" and d["result"] == "incomplete" for d in determinations)
    assert any(d["kind"] == "fbar_missing_values" and d["accounts"] for d in determinations)


def test_open_determinations_include_threshold_uncertainty_and_unidentified_account_labels():
    review = Review.__new__(Review)
    incomplete = {"result": "incomplete", "upper_known": False, "text": "Incomplete. Review"}
    review.worksheet = {
        "totals": {
            "above_threshold": None,
            "threshold_review_reasons": ["accounts_without_value"],
            "crossing_depends_on_flags": False,
            "accounts_without_value": ["ACC2"],
            "unidentified_uploads": [],
        },
        "form8938": {
            "year_end": incomplete,
            "any_time": incomplete,
            "labels": {"year_end": "Year end", "any_time": "Any time", "scope": "Indicative only."},
            "accounts": [
                {
                    "number": "",
                    "key": "unread:1",
                    "uploads": ["pages/unknown.png"],
                    "inclusion": "unresolved",
                },
                {"number": "", "key": "unread:2", "uploads": [], "inclusion": "unresolved"},
            ],
        },
    }
    determinations = review.open_determinations()
    assert any(
        d["kind"] == "fbar_threshold" and d["reasons"] == ["accounts_without_value"] for d in determinations
    )
    year_end = next(d for d in determinations if d["kind"] == "form8938_year_end")
    assert year_end["accounts"] == ["pages/unknown.png", "unread:2"]
    assert "Upper bound is unknown" in year_end["message"]


def test_a_rejected_redraft_replaces_the_previous_draft_with_a_persisted_blocked_state(signed):
    previous = signed.draft_summary(tokens)
    assert previous["text"]
    rejected = signed.draft_summary(lambda phrases, language, fresh: (["bad shape"], {"model": "fake"}))
    assert rejected["blocked"]
    assert rejected["text"] is None
    restarted = Review(signed.inputs, signed.dir, root=signed.dir.parent)
    assert restarted.state["summary"] == rejected
    with pytest.raises(ReviewError, match="can't be released"):
        restarted.approve_summary("Preparer", confirm_unreviewed=True)


def test_unreadable_currency_keeps_a_correction_available_with_one_observed_unit(inputs, tmp_path):
    def mutate(doc):
        for page in doc["pages"]:
            if page["statement_id"] == "kr-checking_2025-12" and "reading" in page:
                page["reading"]["currency"]["text"] = ""
                page["reading"]["currency"]["status"] = "unreadable"

    review = altered_review(inputs, tmp_path, mutate)
    account = next(row for row in review.worksheet["accounts"] if row["account_number"] == "110-482-913746")
    assert account["observed_currencies"] == ["KRW"]
    assert account["currency_review_needed"]
    assert account["max_usd"] is None
    review.set_currency(account["key"], "KRW", "currency verified from the synthetic source", "Preparer")
    corrected = next(row for row in review.worksheet["accounts"] if row["key"] == account["key"])
    assert corrected["max_usd"] == 199100
    assert corrected["currency_correction"]["note"] == "currency verified from the synthetic source"


def test_form8938_panel_uses_distinct_evidence_and_account_keys(inputs):
    panel = build(inputs)["form8938"]
    assert panel["household"]["filing_status"] == "mfs"
    assert panel["intake_source"] == "synthetic intake call script (fictional)"
    checking = next(row for row in panel["accounts"] if row["number"] == "110-482-913746")
    assert checking["key"] in panel["effective_accounts"]
    assert checking["inclusion"] == "included"
    assert checking["rate"]["units_per_usd"] == "1443.75"
    assert checking["rate"]["source"]
    assert checking["year_end_sources"]
    assert all("2025-12" in source["upload"] for source in checking["year_end_sources"])
    assert any(source["ref"] == "closing_balance" for source in checking["year_end_sources"])
    assert any("2025-03" in source["upload"] for source in checking["maximum_sources"])
    assert isinstance(panel["year_end"]["low_usd"], str)
    assert panel["year_end"]["high_usd"] is None
    assert any(row["uploads"] for row in panel["accounts"] if row["inclusion"] == "unresolved")


def test_field_corrections_preserve_other_intake_facts_and_record_source(inputs, tmp_path):
    doc = json.loads(inputs.intake.read_text(encoding="utf-8"))
    account = next(a for a in doc["accounts"] if a["ownership"] == "joint")
    account["joint_with_spouse"] = True
    account["review_metadata"] = "retained"
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    review = Review(Inputs(inputs.set_dir, inputs.readings, path), tmp_path / "review", root=tmp_path)
    key = next(
        k
        for k, item in review.worksheet["form8938"]["effective_accounts"].items()
        if item["account_number"] == account["account_number"]
    )
    review.set_ownership(key, "signature_authority", "corrected from synthetic intake", "Preparer")
    effective = review.worksheet["form8938"]["effective_accounts"][key]
    assert effective["financial_interest"] is True
    assert effective["joint_with_spouse"] is True
    assert effective["metadata"]["review_metadata"] == "retained"
    assert (
        review.worksheet["form8938"]["corrections"]["ownership"][key]["note"]
        == "corrected from synthetic intake"
    )
    flag = next(f for f in review.worksheet["flags"] if f["code"] == "authority_interest_review")
    assert flag["id"] in review.open_review_flags()
    assert "authority_interest_review" not in str(review.summary_facts())


def test_conflicting_ownership_and_interest_needs_review_until_source_correction(review):
    account = next(a for a in review.worksheet["accounts"] if a["ownership"] == "signature_authority")
    review.set_ownership(account["key"], "sole", "synthetic source changed", "Preparer")
    row = next(a for a in review.worksheet["form8938"]["accounts"] if a["key"] == account["key"])
    assert row["inclusion"] == "unresolved"
    assert any(f["code"] == "ownership_interest_conflict" for f in review.worksheet["flags"])
    review.set_financial_interest(account["key"], True, "ownership confirmed from intake", "Preparer")
    row = next(a for a in review.worksheet["form8938"]["accounts"] if a["key"] == account["key"])
    assert row["inclusion"] == "included"
    assert review.worksheet["form8938"]["corrections"]["financial_interest"][account["key"]]["note"]
    assert all(f["code"] != "ownership_interest_conflict" for f in review.worksheet["flags"])


@pytest.mark.parametrize(
    "field,value",
    [("filing_status", "single"), ("lives_abroad", False), ("spouse_specified_individual", True)],
)
def test_each_household_change_invalidates_signed_worksheet(inputs, tmp_path, field, value):
    doc = json.loads(inputs.intake.read_text(encoding="utf-8"))
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    local_inputs = Inputs(inputs.set_dir, inputs.readings, path)
    review = Review(local_inputs, tmp_path / "review", root=tmp_path)
    for fid in review.open_review_flags():
        review.acknowledge(fid, "reviewed", "Preparer")
    review.sign_off("Preparer", "PR")
    doc["household"][field] = value
    path.write_text(json.dumps(doc), encoding="utf-8")
    restarted = Review(local_inputs, review.dir, root=tmp_path)
    assert restarted.state["signoff"] is None
    assert restarted.state["invalidated_signoffs"]


def test_acknowledgement_reopens_when_derived_8938_account_row_changes(review):
    import copy

    flag_id = review.open_review_flags()[0]
    review.acknowledge(flag_id, "reviewed prior context", "Preparer")
    flag = next(item for item in review.worksheet["flags"] if item["id"] == flag_id)
    row = next(item for item in review.worksheet["form8938"]["accounts"] if item["key"] == flag["account"])
    original = copy.deepcopy(row)
    for field in original:
        row[field] = {"changed": field}
        assert flag_id in review.open_review_flags(), field
        row[field] = copy.deepcopy(original[field])
        assert flag_id not in review.open_review_flags(), field


def test_account_correction_reopens_only_that_accounts_acknowledgements(review):
    flags = review.worksheet["flags"]
    first = next(f for f in flags if f["severity"] == "review")
    second = next(f for f in flags if f["severity"] == "review" and f["account"] != first["account"])
    review.acknowledge(first["id"], "reviewed A", "Preparer")
    review.acknowledge(second["id"], "reviewed B", "Preparer")
    row = next(a for a in review.worksheet["form8938"]["accounts"] if a["key"] == first["account"])
    row["reason"] = "changed account A context"
    assert first["id"] in review.open_review_flags()
    assert second["id"] not in review.open_review_flags()
    review.worksheet["form8938"]["household"]["filing_status"] = "single"
    assert second["id"] in review.open_review_flags()


def test_new_shared_worksheet_context_invalidates_existing_acknowledgement(review):
    flag_id = review.open_review_flags()[0]
    review.acknowledge(flag_id, "reviewed before new context", "Preparer")
    review.worksheet["new_shared_context"] = {"added_fact": "material"}
    assert flag_id in review.open_review_flags()
