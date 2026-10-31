"""Synthetic capture runner: no network calls; exercise real recording and worksheet paths."""

import json

import pytest
from PIL import Image

from cross_border_fbar.app.worksheet import Inputs, build
from cross_border_fbar.eval.photo import _case, _rectify, run
from cross_border_fbar.extract.page import FIELD_NAMES
from cross_border_fbar.llm import Client, SpendCapExceeded


def reading():
    box = {"x_min": 100, "y_min": 100, "x_max": 300, "y_max": 200}
    data = {name: {"text": "", "status": "absent", "bbox": box} for name in FIELD_NAMES}
    data["rows"] = [
        {
            "date_text": "2025-01-01",
            "account_text": "",
            "description": "synthetic",
            "amount_text": "10.00",
            "amount_column": "credit",
            "balance_text": "",
            "row_bbox": box,
            "amount_bbox": box,
            "balance_bbox": box,
        }
    ]
    return data


@pytest.mark.parametrize("reference", ["../outside.json", "/tmp/outside.json", "link.json"])
def test_manifest_cannot_read_answer_key_outside_its_directory(tmp_path, reference):
    packet = tmp_path / "packet"
    packet.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (packet / "link.json").symlink_to(outside)
    manifest = packet / "manifest.json"
    manifest.write_text(
        json.dumps({"cases": [{"statement_id": "s", "page": 1, "baseline_truth": reference}]})
    )
    with pytest.raises(ValueError, match="within the manifest directory"):
        _case(manifest, "s", 1)


@pytest.fixture
def capture(tmp_path, monkeypatch):
    image = tmp_path / "synthetic.png"
    Image.new("RGB", (120, 180), "white").save(image)
    target = tmp_path / "review"
    client = Client(
        mode="live", cap_usd=2, ledger=tmp_path / "ledger.jsonl", recordings=target / "recordings"
    )
    sent = []

    def mock_post(body):
        sent.append(body["model"])
        return {
            "choices": [{"message": {"content": json.dumps(reading())}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(client, "_post", mock_post)
    return image, target, client, sent


def test_unscored_capture_records_and_opens_in_review_set(capture):
    image, target, client, sent = capture
    first = run(image, target, synthetic=True, client=client)
    second = run(image, target, synthetic=True, client=client)
    assert sent == ["muse-spark-1.3", "muse-spark-1.3"]
    assert first["geometry_status"] == "unscored"
    assert first["value_summary"] is None
    doc = json.loads((target / "readings.json").read_text())
    assert len(doc["pages"]) == 2
    assert first["capture"] != second["capture"]
    assert (
        target
        / "recordings"
        / second["capture"]
        / "muse-spark-1.3"
        / (second["call"]["request_key"] + ".json")
    ).exists()
    assert (
        target / "recordings" / first["capture"] / "muse-spark-1.3" / (first["call"]["request_key"] + ".json")
    ).exists()
    ws = build(Inputs(target, target / "readings.json", target / "intake.json"))
    assert ws["reader"]["model"] == "muse-spark-1.3"
    flags = [flag for flag in ws["flags"] if flag["code"] == "photo_location_unverified"]
    assert len(flags) == 2
    assert all(flag["severity"] == "review" and flag["sources"][0]["bbox"] is None for flag in flags)
    assert all(
        source["bbox"] is None
        for account in ws["form8938"]["accounts"]
        for source in account["maximum_sources"]
    )
    assert len(list((target / "pages").glob("*.png"))) == 2


def test_manifest_scores_values_but_omits_unverified_geometry(capture, tmp_path):
    image, target, client, _ = capture
    truth = {
        "statements": [
            {
                "statement_id": "synthetic-case",
                "layout": "synthetic",
                "period": "2025-01",
                "page_images": [{"width_px": 120, "height_px": 180}],
                "fields": {},
                "transactions": [
                    {
                        "page": 1,
                        "txn_id": "t1",
                        "date": "2025-01-01",
                        "amount": "10.00",
                        "balance_after": "10.00",
                        "balance_bbox": None,
                        "row_bbox": {"x0": 100, "y0": 100, "x1": 300, "y1": 200},
                        "amount_text_bbox": {"x0": 100, "y0": 100, "x1": 300, "y1": 200},
                    }
                ],
            }
        ]
    }
    (tmp_path / "truth.json").write_text(json.dumps(truth))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"cases": [{"statement_id": "synthetic-case", "page": 1, "baseline_truth": "truth.json"}]})
    )
    result = run(image, target, synthetic=True, client=client, manifest=manifest, case_id="synthetic-case")
    assert result["value_summary"]["fields_value_ok"] == "6/6"
    assert result["value_summary"]["amount_ok"] == "1/1"
    assert result["geometry_summary"] is None
    item = json.loads((target / "readings.json").read_text())["pages"][0]
    assert "row_centre_in_row" not in item["score"]["rows"][0]
    assert "iou_text" not in item["score"]["fields"]["institution"]
    result = run(
        image,
        target,
        synthetic=True,
        client=client,
        manifest=manifest,
        case_id="synthetic-case",
        corners=[(0, 0), (119, 0), (119, 179), (0, 179)],
    )
    assert result["geometry_summary"]["row_centre_in_row"] == "1/1"
    assert "verify alignment" in result["geometry_status"]


def test_failures_do_not_append_partial_readings(capture, monkeypatch):
    image, target, client, sent = capture
    with pytest.raises(ValueError, match="synthetic"):
        run(image, target, synthetic=False, client=client)
    assert sent == []
    run(image, target, synthetic=True, client=client)
    original = (target / "readings.json").read_bytes()
    monkeypatch.setattr(client, "_post", lambda body: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        run(image, target, synthetic=True, client=client)
    assert (target / "readings.json").read_bytes() == original
    assert len(list((target / "pages").glob("*.png"))) == 1


def test_cap_is_finite_and_cannot_exceed_50(monkeypatch, tmp_path):
    monkeypatch.delenv("FBAR_SPEND_CAP_USD", raising=False)
    c = Client(mode="replay", ledger=tmp_path / "ledger")
    assert c.cap_usd == 50
    for invalid in (51, float("nan"), float("inf"), -1):
        with pytest.raises(ValueError, match="spend cap"):
            Client(cap_usd=invalid, ledger=tmp_path / "ledger")
    with pytest.raises(SpendCapExceeded):
        Client(cap_usd=0, ledger=tmp_path / "ledger")._reserve(0.01, "test")


def test_rectify_validates_corner_order():
    image = Image.new("RGB", (10, 10), "white")
    with pytest.raises(ValueError, match="convex"):
        _rectify(image, [(0, 0), (9, 9), (9, 0), (0, 9)], (10, 10))


def test_pdf_page_selection_adds_the_requested_page(capture, tmp_path):
    from fpdf import FPDF

    _, target, client, sent = capture
    pdf_path = tmp_path / "synthetic.pdf"
    pdf = FPDF()
    for label in ("first", "second"):
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(text=label)
    pdf.output(str(pdf_path))
    result = run(pdf_path, target, synthetic=True, client=client, page=2)
    doc = json.loads((target / "readings.json").read_text())
    assert doc["pages"][0]["capture"]["source_page"] == 2
    assert result["value_summary"] is None
    assert sent == ["muse-spark-1.3"]
    with pytest.raises(ValueError, match="outside"):
        run(pdf_path, target, synthetic=True, client=client, page=3)
    assert len(json.loads((target / "readings.json").read_text())["pages"]) == 1


def test_early_input_validation_never_sends_a_call(capture, tmp_path):
    image, target, client, sent = capture
    with pytest.raises(ValueError, match="together"):
        run(image, target, synthetic=True, client=client, manifest=tmp_path / "missing.json")
    with pytest.raises(ValueError, match="require a manifest"):
        run(image, target, synthetic=True, client=client, corners=[(0, 0)] * 4)
    assert not sent
    assert not target.exists()


def test_incompatible_review_set_fails_before_paid_call(capture):
    image, target, client, sent = capture
    target.mkdir()
    (target / "intake.json").write_text(json.dumps({"accounts": []}))
    (target / "readings.json").write_text(json.dumps({"model": "other", "prompt": "old", "pages": []}))
    with pytest.raises(ValueError, match="different model"):
        run(image, target, synthetic=True, client=client)
    assert not sent
    assert not list((target / "pages").glob("*.png"))


def test_malformed_model_response_is_recorded_but_never_added(capture, monkeypatch):
    image, target, client, _ = capture
    bad = reading()
    bad["currency"]["status"] = "invented"
    monkeypatch.setattr(
        client,
        "_post",
        lambda body: {
            "choices": [{"message": {"content": json.dumps(bad)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    with pytest.raises(ValueError, match="invalid string"):
        run(image, target, synthetic=True, client=client)
    assert not (target / "readings.json").exists()
    assert not list((target / "pages").glob("*.png"))
    assert list((target / "recordings").rglob("*.json"))


def test_heic_phone_image_is_supported(capture, tmp_path):
    from pillow_heif import register_heif_opener

    _, target, client, sent = capture
    register_heif_opener()
    image = tmp_path / "synthetic.heic"
    Image.new("RGB", (48, 32), "white").save(image, format="HEIF")
    result = run(image, target, synthetic=True, client=client)
    assert sent == ["muse-spark-1.3"]
    with Image.open(target / "pages" / (result["capture"] + "-p1.png")) as png:
        assert png.size == (48, 32)
