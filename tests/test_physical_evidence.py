"""The published scan and photo readings still produce the counts EVAL.md reports."""

import hashlib
import json
from pathlib import Path

import pytest

from cross_border_fbar.eval.score import score_page

EVIDENCE = Path(__file__).resolve().parents[1] / "evidence" / "physical-capture"
LANGUAGES = ("korean", "german", "thai")
SIX_HEADER_FIELDS = (
    "institution",
    "currency",
    "account_number",
    "holder",
    "opening_balance",
    "closing_balance",
)


def _reading(language: str, condition: str) -> dict:
    path = EVIDENCE / f"{language}-{condition}" / "readings.json"
    return json.loads(path.read_text(encoding="utf-8"))["pages"][0]


@pytest.mark.parametrize("condition", ["scan", "whatsapp"])
def test_each_capture_method_scores_what_eval_reports(inputs, condition):
    truth = json.loads((inputs.set_dir / "truth.json").read_text(encoding="utf-8"))
    statements = {s["statement_id"]: s for s in truth["statements"]}
    header, rows, right = 0, 0, 0
    for language in LANGUAGES:
        page = _reading(language, condition)
        statement = statements[page["capture"]["case"]]
        view = {**statement, "doc_currency": statement["fields"]["currency"]["value"]}
        scored = score_page(view, page["reading"], None)
        header += sum(scored["fields"][name]["value_ok"] for name in SIX_HEADER_FIELDS)
        rows += len(scored["rows"])
        right += sum(
            r["returned"] and r["date_ok"] and r["amount_ok"] and r["balance_ok"] for r in scored["rows"]
        )
    assert (header, rows, right) == (18, 19, 19)


@pytest.mark.parametrize("language", LANGUAGES)
def test_each_photo_is_the_one_that_was_read(language):
    photo = (EVIDENCE / f"{language}-whatsapp" / "photo-as-received.jpeg").read_bytes()
    assert hashlib.sha256(photo).hexdigest() == _reading(language, "whatsapp")["capture"]["source_sha256"]


def test_the_evidence_is_marked_synthetic():
    for language in LANGUAGES:
        for condition in ("scan", "whatsapp"):
            assert _reading(language, condition)["capture"]["synthetic_confirmed"] is True
