"""Correcting a misread amount or balance: audited, recomputed from the correction, original kept."""

import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import ThreadingHTTPServer

import pytest

from cross_border_fbar.app.review import Review, ReviewError
from cross_border_fbar.app.server import make_handler
from cross_border_fbar.app.worksheet import Inputs, page_readings, worksheet_hash
from cross_border_fbar.reconcile.corrections import as_read, line_as_read

MARCH = "kr-checking_2025-03"
MARCH_PAGE = f"pages/{MARCH}-p1.png"
PEAK_ROW = 2  # r3 on the page: the balance is 287,450,200 after the deposit
TRUE_PEAK = "287450200"
MISREAD_PEAK = "287,450,800"
CHECKING = "110-482-913746"


def readings_with(inputs, tmp_path, mutate) -> Inputs:
    doc = json.loads(inputs.readings.read_text(encoding="utf-8"))
    for page in doc["pages"]:
        if "reading" in page:
            mutate(page["statement_id"], page["reading"])
    path = tmp_path / "misread-readings.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return Inputs(inputs.set_dir, path, inputs.intake)


def misread_review(inputs, tmp_path, mutate) -> Review:
    return Review(readings_with(inputs, tmp_path, mutate), tmp_path / "review", root=tmp_path)


def misread_peak(statement, reading):
    if statement == MARCH:
        reading["rows"][PEAK_ROW]["balance_text"] = MISREAD_PEAK


@pytest.fixture
def misread(inputs, tmp_path) -> Review:
    return misread_review(inputs, tmp_path, misread_peak)


@pytest.fixture(scope="module")
def refusing(inputs, tmp_path_factory) -> Review:
    """For tests where the correction is refused and nothing changes; building a review takes seconds."""
    path = tmp_path_factory.mktemp("refusing")
    return misread_review(inputs, path, misread_peak)


def checking(review):
    return next(a for a in review.worksheet["accounts"] if a["account_number"] == CHECKING)


def codes(review, account=None):
    return [f["code"] for f in review.worksheet["flags"] if account is None or f["account"] == account["key"]]


def correct_peak(review, value="287,450,200", note="checked row 3 on the statement", who="A. Rivera"):
    review.correct_value(MARCH_PAGE, f"row:{PEAK_ROW}:balance", value, note, who)


def test_correcting_a_misread_balance_clears_its_flag_and_recomputes_the_highest_balance(misread):
    before = checking(misread)
    assert before["max"]["amount"] == "287450800"
    assert before["max_usd"] == 199101
    assert codes(misread, before).count("arithmetic_mismatch") == 2  # this row and the next
    aggregate = misread.worksheet["totals"]["aggregate_usd"]
    maximum = next(a for a in misread.worksheet["form8938"]["accounts"] if a["key"] == before["key"])[
        "maximum"
    ]

    correct_peak(misread)

    after = checking(misread)
    assert "arithmetic_mismatch" not in codes(misread, after)
    assert after["max"]["amount"] == TRUE_PEAK
    assert after["max_usd"] == 199100
    assert misread.worksheet["totals"]["aggregate_usd"] == aggregate - 1
    corrected = next(a for a in misread.worksheet["form8938"]["accounts"] if a["key"] == after["key"])
    assert Decimal(corrected["maximum"]["low_usd"]) < Decimal(maximum["low_usd"])
    assert maximum["high_usd"] is None  # a line that doesn't add up leaves the upper bound unknown
    assert corrected["maximum"]["high_usd"] is not None


def test_the_corrected_figure_says_so_and_keeps_the_original_reading(misread):
    source_box = checking(misread)["max"]["source"]["bbox"]
    readings_before = misread.inputs.readings.read_bytes()
    correct_peak(misread)
    figure = checking(misread)["max"]
    assert figure["correction"]["read"] == {"text": MISREAD_PEAK, "value": "287450800"}
    assert figure["correction"]["value"] == TRUE_PEAK
    assert figure["correction"]["reviewer"] == "A. Rivera"
    assert figure["source"]["bbox"] == source_box  # correcting a value doesn't move its line
    assert misread.inputs.readings.read_bytes() == readings_before
    row = misread.view()["page_readings"][MARCH_PAGE]["rows"][PEAK_ROW]
    assert row["balance"]["text"] == MISREAD_PEAK
    assert row["balance"]["value"] == "287450800"
    assert row["balance"]["correction"]["value"] == TRUE_PEAK
    assert row["amount"]["correction"] is None


def test_a_correction_is_audited_with_who_original_corrected_and_note(misread):
    correct_peak(misread)
    entry = misread.audit_tail(1)[0]
    assert entry["action"] == "correct_value"
    assert entry["reviewer"] == "A. Rivera"
    assert entry["upload"] == MARCH_PAGE
    assert entry["field"] == f"row:{PEAK_ROW}:balance"
    assert entry["original"] == {"text": MISREAD_PEAK, "value": "287450800"}
    assert entry["before"] == "287450800"
    assert entry["after"] == TRUE_PEAK
    assert entry["note"] == "checked row 3 on the statement"


def test_correcting_an_amount_uses_the_stated_sign_and_clears_the_flag(inputs, tmp_path):
    def wrong_amount(statement, reading):
        if statement == MARCH:
            row = reading["rows"][0]
            row["amount_text"], row["amount_column"] = "500,900", "credit"  # wrong figure and wrong column

    review = misread_review(inputs, tmp_path, wrong_amount)
    assert "arithmetic_mismatch" in codes(review, checking(review))
    review.correct_value(MARCH_PAGE, "row:0:amount", "-500,000", "withdrawal on the statement", "A. Rivera")
    assert "arithmetic_mismatch" not in codes(review, checking(review))
    row = review.view()["page_readings"][MARCH_PAGE]["rows"][0]
    assert row["amount"]["value"] == "500900"  # as read
    assert row["amount"]["correction"]["value"] == "-500000"


def test_a_computed_balance_recomputes_from_a_corrected_amount(inputs, tmp_path):
    """de-joint's first bank prints no running balance, so its balances are computed from the amounts."""
    review = Review(inputs, tmp_path / "review", root=tmp_path)
    upload = "pages/de-joint_2025-01-p1.png"
    page = review.view()["page_readings"][upload]
    last = page["rows"][-1]
    assert last["balance"]["value"] is None
    joint = next(a for a in review.worksheet["accounts"] if a["key"].startswith("DE36"))
    bigger = "9.999.999,99"
    review.correct_value(upload, last["amount"]["field"], bigger, "synthetic check", "A. Rivera")
    joint_after = next(a for a in review.worksheet["accounts"] if a["key"] == joint["key"])
    assert joint_after["max"]["kind"] == "computed"
    assert Decimal(joint_after["max"]["amount"]) > Decimal(joint["max"]["amount"])
    assert joint_after["max"]["source"]["upload"] == upload
    assert joint_after["max"]["correction"]["value"] == "9999999.99"
    assert "arithmetic_mismatch" in [
        f["code"] for f in review.worksheet["flags"] if f["account"] == joint["key"]
    ]  # the statement's own closing balance no longer agrees, so the flag says so


def test_correcting_page_balances_clears_mismatch_and_continuity_flags(inputs, tmp_path):
    def wrong_closing(statement, reading):
        if statement == MARCH:
            reading["closing_balance"]["text"] = "40,657,000"

    review = misread_review(inputs, tmp_path, wrong_closing)
    found = codes(review, checking(review))
    assert "arithmetic_mismatch" in found
    assert "continuity_break" in found
    review.correct_value(MARCH_PAGE, "closing_balance", "40,657,677", "closing as printed", "A. Rivera")
    found = codes(review, checking(review))
    assert "arithmetic_mismatch" not in found
    assert "continuity_break" not in found


def test_a_correction_supplies_a_value_that_could_not_be_read(inputs, tmp_path):
    def unreadable(statement, reading):
        if statement == MARCH:
            reading["opening_balance"].update(text="", status="unreadable")

    review = misread_review(inputs, tmp_path, unreadable)
    assert "field_unreadable" in codes(review, checking(review))
    page = review.view()["page_readings"][MARCH_PAGE]
    assert page["balances"][0] == {
        "field": "opening_balance",
        "wp": "kr-checking · 2025-03-p1 · opening balance",
        "text": "",
        "value": None,
        "correction": None,
    }
    review.correct_value(MARCH_PAGE, "opening_balance", "38,074,000", "opening as printed", "A. Rivera")
    assert "field_unreadable" not in codes(review, checking(review))


def test_a_correction_after_sign_off_is_refused(review_signed):
    with pytest.raises(ReviewError, match="signed off"):
        correct_peak(review_signed, "287,450,201")
    with pytest.raises(ReviewError, match="signed off"):
        review_signed.remove_value_correction(MARCH_PAGE, f"row:{PEAK_ROW}:balance", "A. Rivera")


@pytest.fixture
def review_signed(inputs, tmp_path) -> Review:
    review = Review(inputs, tmp_path / "signed", root=tmp_path)
    for flag in review.open_review_flags():
        review.acknowledge(flag, "checked", "A. Rivera")
    review.sign_off("A. Rivera", "AR")
    return review


def test_a_correction_is_in_the_fingerprint_and_invalidates_a_sign_off_on_reload(review_signed, tmp_path):
    """Corrections are refused after sign-off, so one found in the saved state wasn't what was signed."""
    signed = review_signed.state["signoff"]["hash"]
    assert signed == worksheet_hash(review_signed.worksheet)
    state = json.loads(review_signed.state_path.read_text(encoding="utf-8"))
    field = f"row:{PEAK_ROW}:balance"
    page = page_readings(review_signed.inputs)[MARCH_PAGE]
    state["value_corrections"] = {
        f"{MARCH_PAGE}#{field}": {
            "upload": MARCH_PAGE,
            "field": field,
            "read": as_read(page, field),
            "line": line_as_read(page, field),
            "text": "287,450,201",
            "value": "287450201",
            "note": "added after signing",
            "reviewer": "A. Rivera",
            "ts": 1.0,
        }
    }
    review_signed.state_path.write_text(json.dumps(state), encoding="utf-8")
    reloaded = Review(review_signed.inputs, review_signed.dir, root=tmp_path)
    assert reloaded.state["signoff"] is None
    assert reloaded.state["invalidated_signoffs"][-1]["hash"] == signed
    assert reloaded.audit_tail(1)[0]["action"] == "signoff_invalidated"


def test_a_correction_voids_that_accounts_acknowledgements_only(misread):
    acknowledged = {}
    for flag in misread.worksheet["flags"]:
        if flag["severity"] == "review" and flag["account"] not in acknowledged:
            acknowledged[flag["account"]] = flag["id"]
    key = checking(misread)["key"]
    assert len(acknowledged) > 2
    assert key in acknowledged
    for flag_id in acknowledged.values():
        misread.acknowledge(flag_id, "reviewed", "A. Rivera")
    hashes = {account: misread._account_hash(account) for account in acknowledged}
    correct_peak(misread)
    for account, flag_id in acknowledged.items():
        changed = misread._account_hash(account) != hashes[account]
        assert changed == (account == key), account
        if account != key:
            assert flag_id not in misread.open_review_flags(), account
            assert flag_id in misread.view()["review"]["acks"]


def test_a_flag_that_survives_a_correction_needs_acknowledging_again(inputs, tmp_path):
    """The flag's ID is unchanged, because the other misread still raises the same message; only the
    account's fingerprint says the acknowledgement no longer covers what is there."""

    def two_misreads(statement, reading):
        if statement == MARCH:
            reading["rows"][PEAK_ROW]["balance_text"] = MISREAD_PEAK
        if statement == "kr-checking_2025-07":
            reading["closing_balance"]["text"] = "1,000"

    review = misread_review(inputs, tmp_path, two_misreads)
    july = next(
        f
        for f in review.worksheet["flags"]
        if f["code"] == "arithmetic_mismatch" and "2025-07" in f["sources"][0]["upload"]
    )
    review.acknowledge(july["id"], "reviewed", "A. Rivera")
    assert july["id"] not in review.open_review_flags()
    correct_peak(review)
    assert july["id"] in {f["id"] for f in review.worksheet["flags"]}
    assert july["id"] in review.open_review_flags()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "abc",
        "12abc",
        "1e5",
        "NaN",
        "Infinity",
        "1.5",
        "1234.5",
        "1,2,3",
        "--5",
        "1,234,56",
        "1" * 30,
        None,
        True,
        [],
        {},
    ],
)
def test_input_that_is_not_plainly_a_number_is_rejected(refusing, value):
    entries = len(refusing.audit_tail(100))
    with pytest.raises(ReviewError, match="as printed"):
        refusing.correct_value(MARCH_PAGE, f"row:{PEAK_ROW}:balance", value, "a note", "A. Rivera")
    assert refusing.state["value_corrections"] == {}
    assert len(refusing.audit_tail(100)) == entries


@pytest.mark.parametrize("note", ["", "   \n"])
def test_a_note_is_required(refusing, note):
    with pytest.raises(ReviewError, match="comes from"):
        correct_peak(refusing, note=note)
    assert refusing.state["value_corrections"] == {}


@pytest.mark.parametrize(
    "upload, field",
    [
        ("pages/no-such-page.png", "closing_balance"),
        (MARCH_PAGE, "row:999:amount"),
        (MARCH_PAGE, "row:01:amount"),
        (MARCH_PAGE, "row:0:date"),
        (MARCH_PAGE, "institution"),
        (MARCH_PAGE, "account_number"),
        (["list"], "closing_balance"),
        (MARCH_PAGE, ["list"]),
        (None, None),
    ],
)
def test_only_a_correctable_value_on_a_read_page_can_be_corrected(refusing, upload, field):
    with pytest.raises(ReviewError, match="isn't on a page"):
        refusing.correct_value(upload, field, "1,000", "a note", "A. Rivera")


def test_entering_the_value_as_read_is_refused_and_removing_a_correction_restores_it(misread):
    with pytest.raises(ReviewError, match="as read"):
        correct_peak(misread, value=MISREAD_PEAK)
    flags_before = codes(misread)
    correct_peak(misread)
    correct_peak(misread, value="287,450,300", note="second look")
    assert misread.audit_tail(1)[0]["before"] == TRUE_PEAK  # the previous correction
    assert misread.audit_tail(1)[0]["original"]["value"] == "287450800"  # the original reading is still kept
    misread.remove_value_correction(MARCH_PAGE, f"row:{PEAK_ROW}:balance", "A. Rivera")
    assert misread.state["value_corrections"] == {}
    assert checking(misread)["max"]["amount"] == "287450800"
    assert checking(misread)["max"]["correction"] is None
    assert codes(misread) == flags_before
    entry = misread.audit_tail(1)[0]
    assert (entry["action"], entry["before"], entry["after"]) == (
        "remove_value_correction",
        "287450300",
        "287450800",
    )
    with pytest.raises(ReviewError, match="no correction"):
        misread.remove_value_correction(MARCH_PAGE, f"row:{PEAK_ROW}:balance", "A. Rivera")


def test_corrections_survive_a_restart(misread, tmp_path):
    correct_peak(misread)
    again = Review(misread.inputs, misread.dir, root=tmp_path)
    assert again.worksheet == misread.worksheet
    assert checking(again)["max"]["amount"] == TRUE_PEAK


def test_a_correction_to_a_reading_that_changed_is_retired_on_record(misread, tmp_path):
    correct_peak(misread)
    doc = json.loads(misread.inputs.readings.read_text(encoding="utf-8"))
    for page in doc["pages"]:
        if page["statement_id"] == MARCH:
            page["reading"]["rows"][PEAK_ROW]["balance_text"] = "287,450,900"
    misread.inputs.readings.write_text(json.dumps(doc), encoding="utf-8")
    again = Review(misread.inputs, misread.dir, root=tmp_path)
    assert again.state["value_corrections"] == {}
    assert again.state["stale_value_corrections"][0]["value"] == TRUE_PEAK
    assert checking(again)["max"]["amount"] == "287450900"
    assert again.audit_tail(1)[0]["action"] == "value_correction_stale"


def test_a_correction_does_not_move_to_another_transaction_when_a_row_is_inserted(inputs, tmp_path):
    """Codex on #15: rows are addressed by index and this layout's balances are all empty, so the value at
    that index still read the same after a missed row was recovered, and the correction moved to a different
    transaction. A correction is tied to the whole row it was made on."""
    statement, upload = "de-joint_2025-01", "pages/de-joint_2025-01-p1.png"
    review = Review(inputs, tmp_path / "review", root=tmp_path)
    assert review.view()["page_readings"][upload]["rows"][1]["balance"]["value"] is None
    review.correct_value(upload, "row:1:balance", "1.000,00", "balance from the bank's letter", "A. Rivera")
    assert review.state["value_corrections"]

    doc = json.loads(inputs.readings.read_text(encoding="utf-8"))
    (page,) = [p for p in doc["pages"] if p["statement_id"] == statement]
    rows = page["reading"]["rows"]
    assert rows[0]["balance_text"] == rows[1]["balance_text"] == ""
    assert rows[0]["description"] != rows[1]["description"]
    rows.insert(0, {**rows[0], "description": "a row the first reading missed"})
    changed = tmp_path / "reread.json"
    changed.write_text(json.dumps(doc), encoding="utf-8")

    again = Review(Inputs(inputs.set_dir, changed, inputs.intake), review.dir, root=tmp_path)
    assert again.state["value_corrections"] == {}
    assert again.state["stale_value_corrections"][0]["field"] == "row:1:balance"
    assert again.worksheet["value_corrections"] == []
    assert again.audit_tail(1)[0]["action"] == "value_correction_stale"


def test_a_row_correction_survives_a_change_elsewhere_on_the_page(inputs, tmp_path):
    review = misread_review(inputs, tmp_path, misread_peak)
    correct_peak(review)

    def closing_changed(statement, reading):
        misread_peak(statement, reading)
        if statement == MARCH:
            reading["closing_balance"]["text"] = "40,657,000"

    reread = readings_with(inputs, tmp_path, closing_changed)
    again = Review(reread, review.dir, root=tmp_path)
    assert checking(again)["max"]["amount"] == TRUE_PEAK
    assert list(again.state["value_corrections"]) == [f"{MARCH_PAGE}#row:{PEAK_ROW}:balance"]


def test_a_row_on_a_list_of_several_accounts_belongs_to_the_rows_account(inputs, tmp_path):
    review = Review(inputs, tmp_path / "review", root=tmp_path)
    page = "pages/compiled-ledger-p1.png"
    rows = review.view()["page_readings"][page]["rows"]
    doc = json.loads(inputs.readings.read_text(encoding="utf-8"))
    reading = next(
        p["reading"] for p in doc["pages"] if p["statement_id"] == "compiled-ledger" and p.get("page", 1) == 1
    )
    index = next(i for i, row in enumerate(reading["rows"]) if "*" not in row["account_text"])
    review.correct_value(page, rows[index]["amount"]["field"], "1,000", "synthetic check", "A. Rivera")
    (correction,) = review.worksheet["value_corrections"]
    expected = "".join(ch for ch in reading["rows"][index]["account_text"] if ch.isdigit())
    assert correction["account"] == expected
    assert correction["source"]["ref"] == f"row:{index}"


def test_the_export_carries_corrections_and_their_audit(misread):
    correct_peak(misread)
    for flag in misread.open_review_flags():
        misread.acknowledge(flag, "checked", "A. Rivera")
    misread.sign_off("A. Rivera", "AR")
    exported = misread.export()
    (correction,) = exported["worksheet"]["value_corrections"]
    assert correction["read"]["text"] == MISREAD_PEAK
    assert correction["value"] == TRUE_PEAK
    assert any(entry["action"] == "correct_value" for entry in exported["audit"])


def test_the_server_applies_and_rejects_corrections(misread, inputs):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(misread, inputs.set_dir))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    field = f"row:{PEAK_ROW}:balance"
    try:
        assert post("/api/value", {"upload": MARCH_PAGE, "field": field, "value": "1", "note": ""})[0] == 409
        assert post("/api/value", {"upload": MARCH_PAGE, "field": field, "note": "n"})[0] == 400
        assert post("/api/value", {"upload": [], "field": field, "value": "1", "note": "n"})[0] == 409
        status, view = post(
            "/api/value",
            {
                "upload": MARCH_PAGE,
                "field": field,
                "value": "287,450,200",
                "note": "n",
                "reviewer": "A. Rivera",
            },
        )
        assert status == 200
        assert view["worksheet"]["value_corrections"][0]["reviewer"] == "A. Rivera"
        row = view["page_readings"][MARCH_PAGE]["rows"][PEAK_ROW]
        assert row["balance"]["correction"]["value"] == TRUE_PEAK
        status, view = post(
            "/api/value/remove", {"upload": MARCH_PAGE, "field": field, "reviewer": "A. Rivera"}
        )
        assert status == 200
        assert view["worksheet"]["value_corrections"] == []
        assert post("/api/value/remove", {"upload": MARCH_PAGE, "field": field})[0] == 409
    finally:
        srv.shutdown()
