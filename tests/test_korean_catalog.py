from cross_border_fbar.app import phrases


def test_korean_catalog_review_status_and_terminology():
    current = phrases.current_phrases()
    catalog = phrases.load("ko")
    reviewed = phrases.reviewed("ko", current)
    assert catalog.last_translator == "native Korean speaker"
    assert catalog.revision_date.strftime("%Y-%m-%d") == "2026-10-31"
    assert set(current) - set(reviewed) == {"heading"}  # reworded, so it needs the native reviewer again
    for text in phrases.drafts("ko", current).values():
        assert "잔고" not in text
        assert "계좌 명세서" not in text
        assert "거래내역서" not in text
    assert reviewed["arithmetic_mismatch"] == "판독된 거래 내역과 일치하지 않는 거래명세서상의 잔액"
    assert current["highest_balance"] == "highest observed balance"
    assert (
        current["balance_not_confirmed"] == "highest balance could not be established from the documents read"
    )
    assert phrases.drafts("ko", current)["highest_balance"] == "확인된 최고 잔액"
    assert reviewed["balance_not_confirmed"] == "검토한 서류로는 최고 잔액을 확인할 수 없음"
    printout = phrases.printout("ko")
    assert "native Korean speaker    Date: 2026-10-31" in printout
    assert printout.count("UNREVIEWED") == 1
    assert "1. heading (UNREVIEWED, noun phrase)" in printout
    assert "What your tax preparer reviewed | 세무 대리인이 검토한 항목" in printout
    assert "Review note:" in printout
    assert "공인회계사" not in phrases.drafts("ko", current)["heading"]
    assert "Transcription may be truncated" not in printout


def test_the_reworded_heading_is_a_draft_and_never_reads_as_reviewed():
    current = phrases.current_phrases()
    assert current["heading"] == "What your tax preparer reviewed"
    message = phrases.load("ko").get(current["heading"], context="heading")
    assert message.fuzzy
    assert message.string == "세무 대리인이 검토한 항목"
    assert "heading" not in phrases.reviewed("ko", current)
    assert phrases.drafts("ko", current)["heading"] == "세무 대리인이 검토한 항목"


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
        assert "CPA" not in (static / name).read_text()
