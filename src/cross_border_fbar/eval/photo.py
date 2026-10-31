"""Read one synthetic photo, scan, or PDF page into a local review set.

A manifest case can provide synthetic ground truth. Value scoring works without page geometry;
localization is scored only after the operator supplies the four printed-page corners. No real
person's documents are permitted.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import tempfile
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from ..app.worksheet import Inputs, build
from ..extract.page import PROMPT_VERSION, SCHEMA, read_page
from ..extract.snap import snap_rows
from ..llm import Client
from ..reconcile.intake import IntakeDocument
from .score import score_page, summarize

MODEL = "muse-spark-1.3"


def _open_page(path: Path, page: int) -> Image.Image:
    if page < 1:
        raise ValueError("PDF page must be at least 1")
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(path))
        try:
            if page > len(document):
                raise ValueError("PDF page is outside the document")
            return document[page - 1].render(scale=150 / 72).to_pil().convert("RGB")
        finally:
            document.close()
    if page != 1:
        raise ValueError("non-PDF images have only page 1")
    register_heif_opener()
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def _case(manifest_path: Path, case_id: str, page: int) -> tuple[dict, tuple[int, int]]:
    manifest = json.loads(manifest_path.read_text())
    matches = [c for c in manifest["cases"] if c["statement_id"] == case_id and c["page"] == page]
    if len(matches) != 1:
        raise ValueError("manifest must contain exactly one matching statement and page")
    case = matches[0]
    base = manifest_path.parent.resolve()
    truth_ref = Path(case["baseline_truth"])
    if truth_ref.is_absolute():
        raise ValueError("manifest answer key must be within the manifest directory")
    truth_path = (base / truth_ref).resolve()
    if not truth_path.is_relative_to(base):
        raise ValueError("manifest answer key must be within the manifest directory")
    truth = json.loads(truth_path.read_text())
    statements = [s for s in truth["statements"] if s["statement_id"] == case_id]
    if len(statements) != 1:
        raise ValueError("synthetic answer key does not uniquely identify this case")
    statement = statements[0]
    image = statement["page_images"][page - 1]
    view = {
        **statement,
        "fields": {k: v for k, v in statement["fields"].items() if v["page"] == page},
        "transactions": [t for t in statement["transactions"] if t["page"] == page],
        "doc_currency": (statement["fields"].get("currency") or {}).get("value"),
    }
    return view, (image["width_px"], image["height_px"])


def _solve(matrix: list[list[float]]) -> list[float]:
    """Solve an 8x8 system with pivoting; reject degenerate page corners."""
    for col in range(8):
        pivot = max(range(col, 8), key=lambda row, col=col: abs(matrix[row][col]))
        if abs(matrix[pivot][col]) < 1e-10:
            raise ValueError("page corners are degenerate")
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        divisor = matrix[col][col]
        matrix[col] = [value / divisor for value in matrix[col]]
        for row in range(8):
            if row != col:
                factor = matrix[row][col]
                matrix[row] = [a - factor * b for a, b in zip(matrix[row], matrix[col], strict=True)]
    return [row[-1] for row in matrix]


def _rectify(image: Image.Image, corners: list[tuple[float, float]], size: tuple[int, int]) -> Image.Image:
    """Map a rectangular output page back onto operator-provided photo corners (UL, UR, LR, LL)."""
    if len(corners) != 4 or any(
        not math.isfinite(x) or not math.isfinite(y) or not 0 <= x < image.width or not 0 <= y < image.height
        for x, y in corners
    ):
        raise ValueError("four corners must be finite pixel coordinates inside the input image")
    turns = []
    for i in range(4):
        a, b, c = corners[i], corners[(i + 1) % 4], corners[(i + 2) % 4]
        turns.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    if not all(t > 0 for t in turns):
        raise ValueError(
            "corners must form a convex page in upper-left, upper-right, lower-right, lower-left order"
        )
    width, height = size
    dest = [(0, 0), (width - 1, 0), (width - 1, height - 1), (0, height - 1)]
    matrix = []
    for (x, y), (u, v) in zip(dest, corners, strict=True):
        matrix.extend([[x, y, 1, 0, 0, 0, -u * x, -u * y, u], [0, 0, 0, x, y, 1, -v * x, -v * y, v]])
    return image.transform(size, Image.Transform.PERSPECTIVE, _solve(matrix), Image.Resampling.BICUBIC)


def _value_summary(scored: dict) -> dict:
    fields = list(scored["fields"].values())
    rows = scored["rows"]
    returned = [row for row in rows if row["returned"]]
    result = {
        "fields_value_ok": f"{sum(bool(f['value_ok']) for f in fields)}/{len(fields)}",
        "rows_returned": f"{len(returned)}/{len(rows)}",
        "extra_rows": max(0, scored["rows_returned"] - scored["rows_expected"]),
    }
    for name in ("date_ok", "amount_ok", "balance_ok", "account_ok"):
        result[name] = f"{sum(bool(row[name]) for row in returned)}/{len(rows)}"
    return result


def _strip_geometry(scored: dict) -> dict:
    for field in scored["fields"].values():
        field.pop("centre_in_cell", None)
        field.pop("iou_text", None)
    for row in scored["rows"]:
        row_keys = [key for key in row if "iou" in key or "centre_in" in key]
        for key in row_keys:
            del row[key]
    return scored


def _write_json_atomic(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        try:
            json.dump(value, tmp, ensure_ascii=False, indent=1)
            tmp.write("\n")
            name = tmp.name
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise
    try:
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _validate_object(value, schema, path):
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    for name in schema.get("required", []):
        if name not in value:
            raise ValueError(f"{path}.{name} is missing")
    for name, definition in schema.get("properties", {}).items():
        if name in value:
            _validate_schema(value[name], definition, f"{path}.{name}")
    if {"x_min", "x_max", "y_min", "y_max"} <= value.keys() and (
        value["x_min"] > value["x_max"] or value["y_min"] > value["y_max"]
    ):
        raise ValueError(f"{path} has reversed bounds")


def _validate_array(value, schema, path):
    if not isinstance(value, list):
        raise ValueError(f"{path} must be an array")
    for index, item in enumerate(value):
        _validate_schema(item, schema["items"], f"{path}[{index}]")


def _validate_schema(value, schema, path="reading"):
    """Reject malformed provider responses before adding a page to the review set."""
    kind = schema.get("type")
    if kind == "object":
        _validate_object(value, schema, path)
    elif kind == "array":
        _validate_array(value, schema, path)
    elif kind == "string" and (
        not isinstance(value, str) or ("enum" in schema and value not in schema["enum"])
    ):
        raise ValueError(f"{path} has an invalid string value")
    elif kind == "integer" and (
        type(value) is not int or not schema.get("minimum", value) <= value <= schema.get("maximum", value)
    ):
        raise ValueError(f"{path} has an invalid integer value")


def _existing_set(set_dir: Path, intake: dict, explicit_intake: bool) -> dict:
    intake_file = set_dir / "intake.json"
    readings_file = set_dir / "readings.json"
    if intake_file.exists():
        saved = json.loads(intake_file.read_text())
        IntakeDocument.from_dict(saved)
        if explicit_intake and saved != intake:
            raise ValueError("review set already has different intake facts")
    elif readings_file.exists():
        raise ValueError("existing review set is missing intake.json")
    if not readings_file.exists():
        return {"model": MODEL, "prompt": PROMPT_VERSION, "pages": []}
    doc = json.loads(readings_file.read_text())
    if doc.get("model") != MODEL or doc.get("prompt") != PROMPT_VERSION:
        raise ValueError("review set uses a different model or prompt")
    if not isinstance(doc.get("pages"), list):
        raise ValueError("review set pages must be a list")
    # Catch inaccessible images or malformed existing readings before spending on another call.
    build(Inputs(set_dir, readings_file, intake_file))
    return doc


def _append_capture(set_dir, intake, intake_path, item):
    with (set_dir / ".photo-eval.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        intake_file = set_dir / "intake.json"
        readings_file = set_dir / "readings.json"
        doc = _existing_set(set_dir, intake, bool(intake_path))
        if not intake_file.exists():
            _write_json_atomic(intake_file, intake)
        updated = {**doc, "pages": [*doc["pages"], item]}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=set_dir, delete=False) as tmp:
            json.dump(updated, tmp, ensure_ascii=False)
            preview = Path(tmp.name)
        try:
            build(Inputs(set_dir, preview, intake_file))
            _write_json_atomic(readings_file, updated)
        finally:
            preview.unlink(missing_ok=True)
    return readings_file


def _score_capture(statement, reading, image_path, corners):
    if not statement:
        return None, None, None
    snapped = snap_rows(reading, image_path) if corners else None
    scored = score_page(statement, reading, snapped)
    if not corners:
        scored = _strip_geometry(scored)
    summary = _value_summary(scored)
    geometry = summarize([scored])["all"] if corners else None
    return scored, summary, geometry


def run(
    input_path: Path,
    set_dir: Path,
    *,
    synthetic: bool,
    page: int = 1,
    truth_page: int = 1,
    manifest: Path | None = None,
    case_id: str | None = None,
    corners: list[tuple[float, float]] | None = None,
    intake_path: Path | None = None,
    client: Client | None = None,
) -> dict:
    if not synthetic:
        raise ValueError("confirm that the input is synthetic; real people's documents are not permitted")
    if bool(manifest) != bool(case_id):
        raise ValueError("--manifest and --case must be provided together")
    if corners and not manifest:
        raise ValueError("page corners require a manifest and case for the original page dimensions")
    # The operator explicitly chooses these paths. Resolve them before use; only the indirect
    # answer-key reference inside a manifest is confined to the manifest directory.
    input_path = input_path.resolve(strict=True)
    set_dir = set_dir.resolve()
    manifest = manifest.resolve(strict=True) if manifest else None
    intake_path = intake_path.resolve(strict=True) if intake_path else None
    image = _open_page(input_path, page)
    statement, size = _case(manifest, case_id, truth_page) if manifest else (None, None)
    if corners:
        image = _rectify(image, corners, size)
    intake = (
        json.loads(intake_path.read_text())
        if intake_path
        else {"client": "Synthetic test client", "source": "Not stated", "accounts": []}
    )
    IntakeDocument.from_dict(intake)
    client = client or Client(mode="live")
    if client.mode != "live":
        raise ValueError("photo evaluation requires live extraction")
    set_dir.mkdir(parents=True, exist_ok=True)
    with (set_dir / ".photo-eval.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _existing_set(set_dir, intake, bool(intake_path))
    pages = set_dir / "pages"
    pages.mkdir(exist_ok=True)
    (set_dir / "recordings").mkdir(exist_ok=True)
    capture_id = "capture-" + uuid.uuid4().hex
    image_path = pages / f"{capture_id}-p1.png"
    # The exact raster sent to the model is also the page the worksheet displays.
    image.save(image_path, format="PNG")
    # Each call has a distinct recording directory, even when the exact same image is read again.
    client.recordings = set_dir / "recordings" / capture_id
    try:
        reading, metadata = read_page(client, MODEL, image_path, label=f"photo-eval:{capture_id}")
        _validate_schema(reading, SCHEMA)
        # Validate through the real worksheet pipeline before publishing the new reading.
        from ..reconcile.observe import observe

        observe(reading, f"pages/{image_path.name}")
        scored, summary, geometry = _score_capture(statement, reading, image_path, corners)
        item = {
            "statement_id": capture_id,
            "page": 1,
            "reading": reading,
            "call": metadata,
            "capture": {
                "source_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                "source_page": page,
                "truth_page": truth_page if manifest else None,
                "boxes_verified": False,
                "synthetic_confirmed": True,
                "case": case_id,
                "recording_file": str(client.recordings / MODEL / (metadata["request_key"] + ".json")),
                "corners": corners,
                "geometry": "operator-supplied page corners; verify alignment" if corners else "unscored",
            },
            "score": scored,
            "value_summary": summary,
            "geometry_summary": geometry,
        }
        readings_file = _append_capture(set_dir, intake, intake_path, item)
        return {
            "set": str(set_dir),
            "readings": str(readings_file),
            "capture": capture_id,
            "recording_file": item["capture"]["recording_file"],
            "call": metadata,
            "value_summary": summary,
            "geometry_summary": geometry,
            "geometry_status": item["capture"]["geometry"],
        }
    except BaseException:
        image_path.unlink(missing_ok=True)
        raise


def _corners(text: str) -> list[tuple[float, float]]:
    try:
        pairs = [part.split(",") for part in text.split(";")]
        if len(pairs) != 4 or any(len(pair) != 2 for pair in pairs):
            raise ValueError
        return [(float(x), float(y)) for x, y in pairs]
    except ValueError:
        raise argparse.ArgumentTypeError("corners must be x,y;x,y;x,y;x,y (UL, UR, LR, LL)") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="synthetic image or PDF")
    parser.add_argument("--set", type=Path, default=Path("out/photo-review"))
    parser.add_argument("--confirm-synthetic", action="store_true", required=True)
    parser.add_argument("--page", type=int, default=1, help="1-based PDF page")
    parser.add_argument("--truth-page", type=int, default=1, help="1-based original page in the manifest")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--case")
    parser.add_argument("--corners", type=_corners, help="UL; UR; LR; LL pixel pairs as x,y;x,y;x,y;x,y")
    parser.add_argument("--intake", type=Path, help="optional synthetic intake JSON")
    args = parser.parse_args()
    result = run(
        args.input,
        args.set,
        synthetic=args.confirm_synthetic,
        page=args.page,
        truth_page=args.truth_page,
        manifest=args.manifest,
        case_id=args.case,
        corners=args.corners,
        intake_path=args.intake,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Open: python -m cross_border_fbar.app --set {args.set} --readings {args.set / 'readings.json'}")


if __name__ == "__main__":
    main()
