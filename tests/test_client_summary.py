"""The client note in English and Korean, line by line, and what the preparer releases."""

import re
import socket

import pytest

from cross_border_fbar import llm
from cross_border_fbar.app import phrases as phrase_tables
from cross_border_fbar.app.review import (
    CLIENT_LABELS,
    OUT_OF_SCOPE,
    SUMMARY_PHRASES,
    Review,
    ReviewError,
)

HEADING_DRAFT = "세무 대리인이 검토한 항목"


@pytest.fixture(scope="module")
def signed(inputs, tmp_path_factory) -> Review:
    """A signed worksheet with the committed catalog. Tests redraft, so no test depends on an earlier one."""
    path = tmp_path_factory.mktemp("signed")
    review = Review(inputs, path / "review", root=path)
    for flag_id in review.open_review_flags():
        review.acknowledge(flag_id, "INTERNAL-NOTE-7731", "A. Rivera")
    review.sign_off("A. Rivera", "AR")
    return review


def no_call(*args, **kwargs):
    pytest.fail("the model was called")


@pytest.fixture
def no_model_or_key(monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_api_key", lambda: pytest.fail("API key was accessed"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network was used"))


def test_a_first_draft_needs_no_model_or_key_and_lists_the_unreviewed_catalog_draft(signed, no_model_or_key):
    draft = signed.draft_summary(no_call)
    assert draft["unreviewed"] == {"heading": HEADING_DRAFT}
    assert draft["unreviewed_en"] == {"heading": "What your tax preparer reviewed"}
    assert draft["meta"]["model"] is None
    assert draft["text"].splitlines()[0] == HEADING_DRAFT
    assert draft["text_en"].splitlines()[0] == "What your tax preparer reviewed"


def test_both_languages_have_identical_line_structure(signed, no_model_or_key):
    """Every line is the same fixed phrases and figures: swap each language's phrases for their keys and
    the two versions must be the same text."""
    draft = signed.draft_summary(no_call)
    english = draft["text_en"].splitlines()
    korean = draft["text"].splitlines()
    assert len(english) == len(korean) == len(draft["lines"]) > 1
    assert [line["en"] for line in draft["lines"]] == english
    assert [line["ko"] for line in draft["lines"]] == korean
    table = {**SUMMARY_PHRASES, **CLIENT_LABELS}
    translated = {**phrase_tables.drafts("ko", table)}

    def skeleton(text, phrases):
        for key, phrase in sorted(phrases.items(), key=lambda item: -len(item[1])):
            text = text.replace(phrase, f"«{key}»")
        return text

    for en, ko in zip(english, korean, strict=True):
        assert skeleton(en, table) == skeleton(ko, translated)
    assert any("287,450,200 KRW" in line for line in english)
    assert any("287,450,200 KRW" in line for line in korean)


def test_the_english_note_is_only_fixed_phrases_and_stays_in_scope(signed, no_model_or_key):
    draft = signed.draft_summary(no_call)
    assert "INTERNAL-NOTE-7731" not in draft["text_en"]
    assert OUT_OF_SCOPE.search(draft["text_en"]) is None
    assert not re.search(r"CPA|공인회계사", draft["text_en"] + draft["text"])


@pytest.mark.parametrize(
    "release, korean, english", [("ko", True, False), ("en", False, True), ("both", True, True)]
)
def test_the_release_choice_is_recorded_with_the_approval_and_exported(signed, release, korean, english):
    draft = signed.draft_summary(no_call)
    signed.approve_summary("A. Rivera", confirm_unreviewed=release != "en", release=release)
    approved = signed.state["summary"]["approved"]
    assert approved["release"] == release
    assert approved["reviewer"] == "A. Rivera"
    assert (draft["text"] in approved["client_text"]) is korean
    assert (draft["text_en"] in approved["client_text"]) is english
    assert signed.audit_tail(1)[0]["release"] == release
    exported = signed.export()["client_summary"]
    assert exported["text"] == draft["text"]
    assert exported["text_en"] == draft["text_en"]
    assert exported["lines"] == draft["lines"]
    assert exported["approved"]["release"] == release
    assert exported["approved"]["client_text"] == approved["client_text"]


def test_a_summary_survives_a_restart_with_both_versions_and_the_choice(signed, tmp_path_factory):
    signed.draft_summary(no_call)
    signed.approve_summary("A. Rivera", release="en")
    restarted = Review(signed.inputs, signed.dir, root=signed.dir.parent)
    assert restarted.state["summary"] == signed.state["summary"]
    assert restarted.state["summary"]["approved"]["release"] == "en"


@pytest.mark.parametrize("release", ["ko", "both"])
def test_unreviewed_korean_still_needs_confirmation(signed, release):
    signed.draft_summary(no_call)
    with pytest.raises(ReviewError, match="haven't been reviewed"):
        signed.approve_summary("A. Rivera", release=release)
    assert signed.state["summary"]["approved"] is None
    signed.approve_summary("A. Rivera", confirm_unreviewed=True, release=release)
    assert signed.audit_tail(1)[0]["confirmed_unreviewed"] == ["heading"]
    assert signed.state["summary"]["approved"]["confirmed_unreviewed"] == ["heading"]


def test_an_english_note_needs_no_confirmation(signed):
    signed.draft_summary(no_call)
    signed.approve_summary("A. Rivera", release="en")
    approved = signed.state["summary"]["approved"]
    assert approved["confirmed_unreviewed"] == []
    assert signed.audit_tail(1)[0]["confirmed_unreviewed"] == []


@pytest.mark.parametrize("release", ["", "korean", "fr", None, ["en"], True])
def test_an_unknown_release_choice_is_refused(signed, release):
    signed.draft_summary(no_call)
    with pytest.raises(ReviewError, match="Choose what the client gets"):
        signed.approve_summary("A. Rivera", confirm_unreviewed=True, release=release)
    assert signed.state["summary"]["approved"] is None


@pytest.fixture
def no_catalog(tmp_path, monkeypatch):
    """Every phrase is missing, so Korean depends on the translator."""
    monkeypatch.setattr(phrase_tables, "PHRASES_DIR", tmp_path / "no-tables")


def failing(phrases, language, fresh):
    raise RuntimeError("MODEL_API_KEY is not set")


def unusable(phrases, language, fresh):
    return {key: "신고하셔야 합니다" for key in phrases}, {"model": "fake"}


@pytest.mark.parametrize("translate", [failing, unusable])
def test_english_stays_available_when_korean_cannot_be_produced(signed, no_catalog, translate):
    draft = signed.draft_summary(translate)
    assert draft["blocked"]
    assert draft["text"] is None
    assert draft["text_en"].startswith("What your tax preparer reviewed")
    assert all(line["ko"] is None and line["en"] for line in draft["lines"])
    assert draft["unreviewed"] == {}
    for release in ("ko", "both"):
        with pytest.raises(ReviewError, match="can't be released"):
            signed.approve_summary("A. Rivera", confirm_unreviewed=True, release=release)
    signed.approve_summary("A. Rivera", release="en")
    assert signed.state["summary"]["approved"]["client_text"] == draft["text_en"]


def test_english_comes_from_the_same_tables_the_translation_is_made_from(signed, no_catalog):
    asked = {}

    def spy(phrases, language, fresh):
        asked.update(phrases)
        return {key: f"«{key}»" for key in phrases}, {"model": "fake"}

    draft = signed.draft_summary(spy)
    assert draft["unreviewed_en"] == asked
    for line in draft["lines"]:
        english = line["en"]
        for key, phrase in sorted(asked.items(), key=lambda item: -len(item[1])):
            english = english.replace(phrase, f"«{key}»")
        assert english == line["ko"]
