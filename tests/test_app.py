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
from cross_border_fbar.synth.__main__ import generate

REPO = Path(__file__).resolve().parents[1]
READINGS = REPO / "results" / "muse-spark-1.3_synth.json"


@pytest.fixture(scope="module")
def inputs(tmp_path_factory) -> Inputs:
    out = tmp_path_factory.mktemp("synth")
    generate(out, 2025, png=True, dpi=150, threshold_usd=1000)
    return Inputs(out, READINGS, out / "intake.json")


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


def test_ownership_correction_recomputes_and_adds_its_review_item(review):
    acct = next(a for a in review.worksheet["accounts"] if a["ownership"] == "sole" and a["max"])
    before = len(review.open_review_flags())
    review.set_ownership(acct["key"], "signature_authority", "client clarified by email", "A. Rivera")
    codes = {f["code"] for f in review.worksheet["flags"] if f["account"] == acct["key"]}
    assert "signature_authority_only" in codes
    assert len(review.open_review_flags()) == before + 1
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
    assert "INTERNAL-NOTE-7731" not in json.dumps(seen)  # CPA notes stay internal
    lines = s["text"].splitlines()
    assert lines[0] == "«heading»"
    assert "새별은행 · «account_ending» 3746 · «highest_balance» 287,450,200 KRW ($199,100)" in lines
    assert "  «reviewed»: «nothing_further»" in lines
    # Every word outside the figures and bank names is a fixed phrase.
    words = set(re.findall(r"[A-Za-z가-힣]+", re.sub(r"«[a-z_]+»", "", s["text"])))
    banks = {
        w
        for a in signed.worksheet["accounts"]
        for n in a["institutions"]
        for w in re.findall(r"[A-Za-z가-힣]+", n)
    }
    assert words - banks <= {"KRW", "EUR", "THB"}
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
    ],
)
def test_a_translation_that_adds_anything_is_held_back(signed, bad):
    s = signed.draft_summary(lambda phrases, language, fresh: (bad(phrases), {"model": "fake"}))
    assert s["blocked"] and s["text"] is None
    with pytest.raises(ReviewError, match="can't be released"):
        signed.approve_summary("A. Rivera", confirm_unreviewed=True)


def test_unreviewed_phrases_are_approved_only_when_confirmed(signed):
    """Codex on #5: 'you should file an FBAR' passed the word check. A phrase no one has reviewed is shown
    to the CPA and needs explicit confirmation; the reviewed table is the normal path."""

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
    table = {k: {"en": en, "text": f"[{k}]"} for k, en in {**SUMMARY_PHRASES, **CLIENT_LABELS}.items()}
    d = tmp_path / "tables"
    d.mkdir()
    (d / "ko.json").write_text(json.dumps({"reviewed_by": "A. Reviewer", "phrases": table}))
    monkeypatch.setattr(phrase_tables, "PHRASES_DIR", d)

    def no_call(*a, **k):
        raise AssertionError("a reviewed table needs no translation")

    s = signed.draft_summary(no_call)
    assert s["unreviewed"] == {} and s["text"].startswith("[heading]")
    signed.approve_summary("A. Rivera")
    table["heading"]["en"] = "an older English heading"
    (d / "ko.json").write_text(json.dumps({"reviewed_by": "A. Reviewer", "phrases": table}))
    s = signed.draft_summary(tokens)
    assert list(s["unreviewed"]) == ["heading"]


def test_the_committed_korean_table_covers_every_phrase_with_current_english():
    """Changing an English phrase or adding a review flag makes the table stale; this fails until it is
    translated and reviewed again."""
    doc = json.loads(phrase_tables.table_path("ko").read_text())
    entries = doc["phrases"]
    current = {**SUMMARY_PHRASES, **CLIENT_LABELS}
    assert {k for k in current if k not in entries or entries[k]["en"] != current[k]} == set()


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
    """Codex review on #5: a restart with changed readings or intake kept the old sign-off."""
    first = Review(inputs, tmp_path / "rv", root=tmp_path)
    for fid in first.open_review_flags():
        first.acknowledge(fid, "checked", "A. Rivera")
    first.sign_off("A. Rivera", "AR")
    intake = json.loads(inputs.intake.read_text())
    intake["accounts"][0]["ownership"] = "joint"  # the figures that were signed no longer match
    changed = tmp_path / "intake-changed.json"
    changed.write_text(json.dumps(intake))
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
    """Codex review on #5: polite Korean inflections slipped past the literal patterns."""
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
    """Codex review on #5: same-code flags without month or sources shared one ID."""
    from cross_border_fbar.app.worksheet import flag_id
    from cross_border_fbar.reconcile.accounts import Flag

    a = Flag("coverage_gap", "review", "no statement covers 2025-01-01 to 2025-01-14")
    b = Flag("coverage_gap", "review", "no statement covers 2025-12-16 to 2025-12-31")
    assert flag_id("ACC1", a) != flag_id("ACC1", b)


def test_every_review_flag_has_a_client_label_and_a_worksheet_title():
    """A review flag without a label reached the client summary as its raw code. Read the codes from the
    reconciler's source so a new flag can't be added without both."""
    import re
    from pathlib import Path

    from cross_border_fbar.app.review import CLIENT_LABELS

    pkg = Path(__file__).resolve().parents[1] / "src" / "cross_border_fbar"
    source = "".join(p.read_text() for p in (pkg / "reconcile").glob("*.py"))
    review_codes = set(re.findall(r'Flag\(\s*"([a-z_]+)",\s*"review"', source))
    assert {"institution_names_differ", "possible_intraday_peak", "ownership_unknown"} <= review_codes
    assert review_codes - set(CLIENT_LABELS) == set()
    app_js = (pkg / "app" / "static" / "app.js").read_text()
    titled = set(re.findall(r"^\s+([a-z_]+): \(", app_js, re.M))
    assert review_codes - titled == set()


def test_failed_pages_and_accounts_without_statements_block_sign_off(inputs, tmp_path):
    """Codex on #5: pages whose extraction failed were dropped, so a readings file of failures signed off
    as an empty $0 worksheet. Each failed page and each intake account with nothing read is a review item."""
    readings = json.loads(READINGS.read_text())
    failed = {"statement_id": "kr-checking_2025-03", "page": 1, "error": "HTTP 503 from the model API"}
    readings["pages"] = [failed] + [
        p for p in readings["pages"] if not p["statement_id"].startswith("kr-checking")
    ]
    path = tmp_path / "readings.json"
    path.write_text(json.dumps(readings))
    ws = build(Inputs(inputs.set_dir, path, inputs.intake))
    codes = {(f["code"], f["account"]) for f in ws["flags"]}
    assert ("page_not_read", "unread:pages/kr-checking_2025-03-p1.png") in codes
    (page_flag,) = [f for f in ws["flags"] if f["code"] == "page_not_read"]
    assert page_flag["severity"] == "review" and "HTTP 503" in page_flag["message"]
    assert page_flag["sources"][0]["upload"] == "pages/kr-checking_2025-03-p1.png"
    (kr,) = [a for a in ws["accounts"] if a["account_number"] == "110-482-913746"]
    assert kr["max"] is None and kr["identified"]
    assert [f["code"] for f in ws["flags"] if f["account"] == kr["key"]] == ["no_statements"]
    assert "110-482-913746" in ws["totals"]["accounts_without_value"]
    assert "pages/kr-checking_2025-03-p1.png" in ws["totals"]["unidentified_uploads"]

    review = Review(Inputs(inputs.set_dir, path, inputs.intake), tmp_path / "review", root=tmp_path)
    with pytest.raises(ReviewError, match="still need"):
        review.sign_off("A. Rivera", "AR")


def test_only_failed_pages_still_show_every_intake_account(inputs, tmp_path):
    readings = json.loads(READINGS.read_text())
    readings["pages"] = [
        {"statement_id": p["statement_id"], "page": p.get("page", 1), "error": "timeout"}
        for p in readings["pages"]
    ]
    path = tmp_path / "readings.json"
    path.write_text(json.dumps(readings))
    ws = build(Inputs(inputs.set_dir, path, inputs.intake))
    intake_numbers = {a["account_number"] for a in json.loads(inputs.intake.read_text())["accounts"]}
    assert {a["account_number"] for a in ws["accounts"] if a["identified"]} == intake_numbers
    assert sum(f["code"] == "page_not_read" for f in ws["flags"]) == len(readings["pages"])
    assert ws["totals"]["aggregate_usd"] == 0


def test_every_worksheet_field_is_in_the_signed_fingerprint(inputs):
    """Codex on #5: the fingerprint listed its fields, so the client's name could change under a sign-off.
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
    intake = json.loads(inputs.intake.read_text())
    intake["client"] = "Another Client"
    changed = tmp_path / "intake-client.json"
    changed.write_text(json.dumps(intake))
    again = Review(Inputs(inputs.set_dir, inputs.readings, changed), tmp_path / "rv", root=tmp_path)
    assert again.state["signoff"] is None and again.state["summary"] is None
    with pytest.raises(ReviewError):
        again.export()


@pytest.mark.parametrize("language", ["../../outside", "/tmp/x", "ko/../..", "en"])
def test_a_phrase_table_path_takes_only_a_listed_language(language):
    """SonarCloud on #5: the language from the command line reached a file path."""
    with pytest.raises(ValueError, match="no client language"):
        phrase_tables.table_path(language)


def test_only_this_apps_own_page_can_act_or_read(inputs, tmp_path):
    """Codex on #5: another site open in the CPA's browser could POST a sign-off to localhost, and a
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
    """Codex on #5: rows without an account number shared an empty key, so two of them missing the same
    month got one review ID and one acknowledgement covered both."""
    readings = json.loads(READINGS.read_text())
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
    path.write_text(json.dumps(readings))
    for ws in (build(inputs), build(Inputs(inputs.set_dir, path, inputs.intake))):
        keys = [a["key"] for a in ws["accounts"]]
        ids = [f["id"] for f in ws["flags"]]
        assert len(keys) == len(set(keys))
        assert len(ids) == len(set(ids))
    assert sum(1 for a in ws["accounts"] if a["key"].startswith("unidentified:")) == 2


def test_the_eval_does_not_count_intake_rows_as_found(inputs, tmp_path, monkeypatch):
    """Codex on #5: a row for every intake account made accounts_found perfect even with no page read."""
    from cross_border_fbar.eval import reconcile_eval

    readings = json.loads(READINGS.read_text())
    readings["pages"] = [
        {"statement_id": p["statement_id"], "page": p.get("page", 1), "error": "x"} for p in readings["pages"]
    ]
    results = tmp_path / "results"
    results.mkdir()
    (results / f"muse-spark-1.3_{inputs.set_dir.name}.json").write_text(json.dumps(readings))
    monkeypatch.setattr(reconcile_eval, "RESULTS", results)
    out = reconcile_eval.evaluate(inputs.set_dir, "muse-spark-1.3")
    assert out["summary"]["accounts_found"].startswith("0/")
