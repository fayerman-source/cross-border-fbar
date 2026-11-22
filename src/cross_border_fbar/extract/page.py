"""Read one statement page with a Meta model: text and boxes out, nothing computed."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from ..llm import Client

PROMPT_VERSION = "statement_page_v3"
PROMPT = (Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")

_BOX = {
    "type": "object",
    "properties": {
        k: {"type": "integer", "minimum": 0, "maximum": 1000} for k in ("x_min", "y_min", "x_max", "y_max")
    },
    "required": ["x_min", "y_min", "x_max", "y_max"],
}
FIELD_NAMES = (
    "institution",
    "currency",
    "account_number",
    "holder",
    "period",
    "opening_balance",
    "closing_balance",
)
_FIELD = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "status": {"type": "string", "enum": ["read", "absent", "unreadable"]},
        "bbox": _BOX,
    },
    "required": ["text", "status", "bbox"],
}
SCHEMA = {
    "type": "object",
    "properties": {
        **{f: {**_FIELD} for f in FIELD_NAMES},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date_text": {"type": "string"},
                    "account_text": {"type": "string"},
                    "description": {"type": "string"},
                    "amount_text": {"type": "string"},
                    "amount_column": {"type": "string", "enum": ["debit", "credit", "signed"]},
                    "balance_text": {"type": "string"},
                    "row_bbox": _BOX,
                    "amount_bbox": _BOX,
                    "balance_bbox": _BOX,
                },
                "required": [
                    "date_text",
                    "account_text",
                    "description",
                    "amount_text",
                    "amount_column",
                    "balance_text",
                    "row_bbox",
                    "amount_bbox",
                    "balance_bbox",
                ],
            },
        },
    },
    "required": [*FIELD_NAMES, "rows"],
}


def read_page(client: Client, model: str, image: Path, label: str = "") -> tuple[dict, dict]:
    """Return (reading, call metadata). The image must be the exact file whose grid the boxes refer to."""
    b64 = base64.b64encode(image.read_bytes()).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }
    ]
    fmt = {"type": "json_schema", "json_schema": {"name": "StatementPage", "schema": SCHEMA}}
    r = client.chat(model, messages, max_tokens=16000, response_format=fmt, label=label)
    meta = {
        "model": model,
        "prompt": PROMPT_VERSION,
        "cost_usd": r.cost_usd,
        "latency_s": r.latency_s,
        "replayed": r.replayed,
        "request_key": r.key,
        "usage": r.usage,
    }
    return json.loads(r.content), meta


def read_statement_source(
    source: Path,
    *,
    page: int = 1,
    client: Client | None = None,
    model: str = "muse-spark-1.3",
    image: Path | None = None,
    label: str = "",
) -> tuple[dict, dict]:
    """Parse CSV or text-layer PDF locally at $0 when unambiguous; otherwise fall back to `read_page`."""
    import hashlib
    import time

    from .digital import read_csv_statement, read_digital_pdf_page

    suffix = source.suffix.lower()
    t0 = time.perf_counter()
    if suffix == ".csv":
        if page != 1:
            raise ValueError("CSV statements have only page 1")
        reading = read_csv_statement(source)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return reading, {
            "model": "digital-csv",
            "prompt": "digital_v1",
            "cost_usd": 0.0,
            "latency_s": round(time.perf_counter() - t0, 4),
            "replayed": False,
            "request_key": digest,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
    if suffix == ".pdf":
        reading = read_digital_pdf_page(source, page=page)
        if reading is not None:
            digest = hashlib.sha256(source.read_bytes() + f":{page}".encode()).hexdigest()
            return reading, {
                "model": "digital-pdf",
                "prompt": "digital_v1",
                "cost_usd": 0.0,
                "latency_s": round(time.perf_counter() - t0, 4),
                "replayed": False,
                "request_key": digest,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            }
    if client is None or image is None:
        raise ValueError("vision fallback requires both a model client and a rendered page image")
    return read_page(client, model, image, label=label)
