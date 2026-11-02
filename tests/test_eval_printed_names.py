"""The answer key lists every bank name a page prints. A reader may return any of them, and nothing else."""

import json
import shutil
from pathlib import Path

import pytest

from cross_border_fbar.eval import reconcile_eval
from cross_border_fbar.eval.score import _score_fields, reads_printed_name

REPO_RESULTS = Path(__file__).resolve().parents[1] / "results"
THAI_NAMES = ["ธนาคารสยามโลตัส", "Siam Lotus Bank"]
BOX = {"x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1}
BOXES = {"bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}, "text_bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}}


@pytest.mark.parametrize(
    "text",
    [
        "ธนาคารสยามโลตัส",
        "Siam Lotus Bank",
        "siam  lotus bank",
        "ธนาคารสยามโลตัส Siam Lotus Bank",
        "ธนาคารสยามโลตัส / Siam Lotus Bank",
        "Siam Lotus Bank (ธนาคารสยามโลตัส)",
    ],
)
def test_any_printed_name_or_several_is_a_correct_read(text):
    assert reads_printed_name(text, THAI_NAMES)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "/", "Shinhan Bank", "Siam Bank", "Siam Lotus Bank / Shinhan", "Siam Lotus Bank Thailand"],
)
def test_empty_text_a_wrong_name_or_an_addition_to_a_name_is_not(text):
    assert not reads_printed_name(text, THAI_NAMES)


def test_a_key_with_no_names_accepts_nothing():
    assert not reads_printed_name("Siam Lotus Bank", [])
    assert not reads_printed_name("Siam Lotus Bank", [""])


def test_the_key_lists_only_names_actually_printed_on_the_page(inputs):
    """name_en exists for every bank but only Thai statements print it, and the reader may only be credited
    for what is on the page."""
    import pypdfium2 as pdfium

    truth = json.loads((inputs.set_dir / "truth.json").read_text())
    for statement in truth["statements"]:
        if statement["bank"] is None:
            assert "institution" not in statement["fields"]  # the compiled list prints no bank name
            continue
        names = statement["fields"]["institution"]["printed_names"]
        english = {"th": ["Siam Lotus Bank"]}.get(statement["layout"], [])
        assert names == [statement["bank"], *english]
    thai, korean = (
        next(s for s in truth["statements"] if s["layout"] == layout)["file"] for layout in ("th", "kr")
    )

    def page_text(file):
        document = pdfium.PdfDocument(str(inputs.set_dir / file))
        try:
            return document[0].get_textpage().get_text_range()
        finally:
            document.close()

    assert "Siam Lotus Bank" in page_text(thai)
    assert "Saebyeol Bank" not in page_text(korean)


def test_scoring_accepts_the_printed_english_name_and_requires_status_read():
    statement = {
        "fields": {"institution": {"value": THAI_NAMES[0], "printed_names": THAI_NAMES, **BOXES}},
    }

    def score(text, status="read"):
        reading = {
            name: {"text": "", "status": "absent", "bbox": BOX}
            for name in ("currency", "account_number", "holder", "opening_balance", "closing_balance")
        }
        reading["institution"] = {"text": text, "status": status, "bbox": BOX}
        return _score_fields(statement, reading)["institution"]["value_ok"]

    assert score("Siam Lotus Bank")
    assert score(THAI_NAMES[0])
    assert not score("Shinhan Bank")
    assert not score("")
    assert not score("Siam Lotus Bank", status="absent")


def evaluated(inputs, tmp_path, monkeypatch, model, names=None):
    """Run the reconciliation eval on a model's recorded readings, with bank names replaced on some pages.
    Each changed page is rescored the way the extraction eval would have scored it."""
    results = tmp_path / "results"
    results.mkdir()
    target = results / f"{model}_{inputs.set_dir.name}.json"
    shutil.copyfile(REPO_RESULTS / f"{model}_synth.json", target)
    if names:
        truth = {
            s["statement_id"]: s
            for s in json.loads((inputs.set_dir / "truth.json").read_text())["statements"]
        }
        doc = json.loads(target.read_text())
        for page in doc["pages"]:
            if page["statement_id"] in names:
                page["reading"]["institution"]["text"] = names[page["statement_id"]]
                page["fields"]["institution"] = _score_fields(
                    {"fields": {"institution": truth[page["statement_id"]]["fields"]["institution"]}},
                    page["reading"],
                )["institution"]
        target.write_text(json.dumps(doc))
    monkeypatch.setattr(reconcile_eval, "RESULTS", results)
    return reconcile_eval.evaluate(inputs.set_dir, model)


def test_glimmers_recorded_thai_flag_is_counted_as_a_printed_name_choice(inputs, tmp_path, monkeypatch):
    """Recorded readings of th-savings return the Thai name, the English name and both across pages."""
    result = evaluated(inputs, tmp_path, monkeypatch, "muse-glimmer")
    flags = result["summary"]["flags"]
    assert flags == {
        "true_positive": 9,
        "false_positive": 0,
        "missed": 0,
        "raised_by_misreads": 0,
        "raised_by_printed_name_choice": 1,
    }
    burden = result["summary"]["review_burden"]
    assert burden["review_items_total"] == 7  # the item stays in the reviewer's workload
    assert burden["additional_items_from_printed_name_choice"] == 1
    assert burden["additional_items_other"] == 0
    thai = next(a for a in result["accounts"] if a["account_id"] == "th-savings")
    assert thai["flags_from_printed_name_choice"] == {
        "institution_names_differ": ["th-savings_2025-01", "th-savings_2025-10", "th-savings_2025-11"]
    }
    assert reconcile_eval.passes(result)


def test_a_clean_reading_of_one_printed_name_raises_nothing(inputs, tmp_path, monkeypatch):
    result = evaluated(inputs, tmp_path, monkeypatch, "muse-spark-1.3")
    assert result["summary"]["flags"]["raised_by_printed_name_choice"] == 0
    assert reconcile_eval.passes(result)


@pytest.mark.parametrize(
    "statement, name",
    [
        ("th-savings_2025-05", "Shinhan Bank"),  # invented
        ("th-savings_2025-05", "Siam Lotus Bank / Shinhan"),  # a printed name with an invented one added
        ("kr-checking_2025-05", "Saebyeol Bank"),  # this bank's name_en, which Korean pages don't print
        ("kr-checking_2025-05", "다온은행"),  # a real name in the set, printed on another account's pages
    ],
)
def test_a_name_the_page_does_not_print_still_fails_the_gate(inputs, tmp_path, monkeypatch, statement, name):
    result = evaluated(inputs, tmp_path, monkeypatch, "muse-spark-1.3", {statement: name})
    flags = result["summary"]["flags"]
    assert flags["false_positive"] == 1
    assert flags["raised_by_printed_name_choice"] == 0
    account = next(a for a in result["accounts"] if a["flags_extra"])
    assert account["flags_extra"] == [("institution_names_differ", "")]
    assert account["unscored_review_flags"] == ["institution_names_differ"]
    assert not reconcile_eval.passes(result)


def test_one_wrong_name_is_not_excused_by_another_page_choosing_a_printed_name(inputs, tmp_path, monkeypatch):
    names = {"th-savings_2025-05": "Siam Lotus Bank", "th-savings_2025-06": "Shinhan Bank"}
    result = evaluated(inputs, tmp_path, monkeypatch, "muse-spark-1.3", names)
    assert result["summary"]["flags"]["false_positive"] == 1
    assert not reconcile_eval.passes(result)


def test_a_printed_name_chosen_on_one_page_is_a_name_choice_not_a_false_positive(
    inputs, tmp_path, monkeypatch
):
    result = evaluated(
        inputs, tmp_path, monkeypatch, "muse-spark-1.3", {"th-savings_2025-05": "Siam Lotus Bank"}
    )
    assert result["summary"]["flags"]["false_positive"] == 0
    assert result["summary"]["flags"]["raised_by_printed_name_choice"] == 1
    assert reconcile_eval.passes(result)
