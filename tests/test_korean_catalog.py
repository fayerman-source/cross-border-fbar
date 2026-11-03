import pytest

from cross_border_fbar.app import phrases
from cross_border_fbar.app.review import _translation_problem


@pytest.mark.committed_catalog
def test_the_committed_catalog_is_machine_translated_and_marked_unreviewed():
    current = phrases.current_phrases()
    catalog = phrases.load("ko")
    assert catalog.last_translator == ""
    assert phrases.reviewed("ko", current) == {}
    assert set(phrases.drafts("ko", current)) == set(current)
    assert _translation_problem(current, phrases.drafts("ko", current)) is None
    printout = phrases.printout("ko")
    assert "(reviewed," not in printout
    assert printout.count("UNREVIEWED") == len(current)


@pytest.mark.committed_catalog
def test_the_catalog_keeps_no_reviewer_notes():
    path = phrases.table_path("ko")
    text = path.read_text(encoding="utf-8")
    for note in ("Reviewed by", "Review note", "native", "Terminology rule"):
        assert note not in text


def test_a_fuzzy_entry_is_a_draft_not_a_reviewed_phrase(heading_unreviewed):
    current = phrases.current_phrases()
    assert "heading" not in phrases.reviewed("ko", current)
    assert phrases.drafts("ko", current)["heading"] == heading_unreviewed


def test_no_user_facing_wording_says_cpa():
    from pathlib import Path

    from cross_border_fbar.app.form8938 import LABELS
    from cross_border_fbar.app.review import CLIENT_LABELS, SUMMARY_PHRASES
    from cross_border_fbar.app.worksheet import SCOPE_LINE
    from cross_border_fbar.reconcile.flags import CATALOG

    catalog = [
        text for d in CATALOG.values() for text in (d.condition, d.cpa_action, d.title, d.client_phrase)
    ]
    fixed = [SCOPE_LINE, *LABELS.values(), *SUMMARY_PHRASES.values(), *CLIENT_LABELS.values(), *catalog]
    assert not [text for text in fixed if "CPA" in text]
    static = Path(phrases.__file__).parent / "static"
    for name in ("app.js", "index.html"):
        assert "CPA" not in (static / name).read_text(encoding="utf-8")
