"""The review app: worksheet build from recorded readings, review rules, scope guard, server routes."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cross_border_fbar.app.review import Review, ReviewError
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
    for fid in review.open_review_flags():
        review.acknowledge(fid, "checked", "A. Rivera")
    review.sign_off("A. Rivera", "AR")
    assert review.state["signoff"]["hash"] == worksheet_hash(review.worksheet)
    with pytest.raises(ReviewError, match="signed off"):
        review.set_ownership(review.worksheet["accounts"][0]["key"], "joint", "late", "A. Rivera")
    actions = [e["action"] for e in review.audit_tail(100)]
    assert actions.count("acknowledge") == 5
    assert actions[-1] == "sign_off"


def test_ownership_correction_recomputes_and_adds_its_review_item(review):
    acct = next(a for a in review.worksheet["accounts"] if a["ownership"] == "sole" and a["max"])
    before = len(review.open_review_flags())
    review.set_ownership(acct["key"], "signature_authority", "client clarified by email", "A. Rivera")
    codes = {f["code"] for f in review.worksheet["flags"] if f["account"] == acct["key"]}
    assert "signature_authority_only" in codes
    assert len(review.open_review_flags()) == before + 1
    assert review.audit_tail(1)[0]["after"] == "signature_authority"


def test_summary_only_after_sign_off_and_out_of_scope_drafts_are_held(review):
    with pytest.raises(ReviewError, match="Sign off"):
        review.draft_summary(lambda facts: ("x", {}))
    for fid in review.open_review_flags():
        review.acknowledge(fid, "INTERNAL-NOTE-7731", "A. Rivera")
    review.sign_off("A. Rivera", "AR")
    facts_seen = {}

    def bad(facts):
        facts_seen.update(facts)
        return "CPA가 검토한 내용\n이 계좌는 신고해야 합니다.", {"model": "fake"}

    review.draft_summary(bad)
    assert review.state["summary"]["blocked"]
    with pytest.raises(ReviewError, match="can't be released"):
        review.approve_summary("A. Rivera")
    assert "INTERNAL-NOTE-7731" not in json.dumps(facts_seen, ensure_ascii=False)  # CPA notes stay internal
    review.draft_summary(lambda facts: ("CPA가 검토한 내용\n새별은행 끝자리 3746 계좌.", {"model": "fake"}))
    review.approve_summary("A. Rivera")
    assert review.state["summary"]["approved"]["reviewer"] == "A. Rivera"


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
