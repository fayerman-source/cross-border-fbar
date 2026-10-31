from cross_border_fbar.app import phrases


def test_korean_catalog_review_status_and_terminology():
    current = phrases.current_phrases()
    catalog = phrases.load("ko")
    reviewed = phrases.reviewed("ko", current)
    assert catalog.last_translator == "native Korean speaker"
    assert catalog.revision_date.strftime("%Y-%m-%d") == "2026-10-30"
    unreviewed_numbers = {3, 6, 11, 12, 13, 14, 21, 22}
    assert set(reviewed) == {key for n, key in enumerate(current, 1) if n not in unreviewed_numbers}
    for text in phrases.drafts("ko", current).values():
        assert "잔고" not in text
        assert "계좌 명세서" not in text
        assert "거래내역서" not in text
    assert "거래내역과 거래명세서상의" in phrases.drafts("ko", current)["arithmetic_mismatch"]
    assert current["highest_balance"] == "highest observed balance"
    assert current["balance_not_confirmed"] == "highest observed balance not determined"
    assert phrases.drafts("ko", current)["highest_balance"] == "확인된 최고 잔액"
    assert phrases.drafts("ko", current)["balance_not_confirmed"] == "확인된 최고 잔액 산정 불가"
    printout = phrases.printout("ko")
    assert "native Korean speaker    Date: 2026-10-30" in printout
    assert "Transcription may be truncated; confirm item 14" in printout
    assert printout.count("Confirm rows versus columns with reviewer") == 2
