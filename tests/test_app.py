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
    return Review(inputs, tmp_path / "review")


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
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Review(inputs, tmp_path / "r"), inputs.set_dir))
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
