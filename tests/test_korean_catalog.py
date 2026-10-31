from cross_border_fbar.app import phrases


def test_korean_catalog_review_status_and_terminology():
    current = phrases.current_phrases()
    catalog = phrases.load("ko")
    reviewed = phrases.reviewed("ko", current)
    assert catalog.last_translator == "native Korean speaker"
    assert catalog.revision_date.strftime("%Y-%m-%d") == "2026-10-31"
    assert set(reviewed) == set(current)
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
    assert "UNREVIEWED" not in printout
    assert "Transcription may be truncated" not in printout
