"""Extraction accuracy on the synthetic set.

    python -m cross_border_fbar.eval --set out/synth [--mode replay|live|auto] [--model muse-spark-1.3]

replay (default) needs no key and no network: it uses committed recordings and
fails on any page that wasn't recorded. live/auto call the API under the spend
cap and record what they get. Results go to results/<model>_<set>.json.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..extract.page import PROMPT_VERSION, read_page
from ..extract.snap import snap_fields, snap_rows
from ..llm import Client, NotRecorded
from .score import score_page, summarize

RESULTS = Path(__file__).resolve().parents[3] / "results"


def evaluate(set_dir: Path, model: str, mode: str, workers: int, limit: int | None) -> dict:
    truth = json.loads((set_dir / "truth.json").read_text(encoding="utf-8"))
    statements = truth["statements"]
    if limit:
        statements = statements[:limit]
    client = Client(mode=mode)

    def page_view(st: dict, page: int) -> dict:
        """The fields and rows printed on one page of a document."""
        return {
            **st,
            "fields": {k: v for k, v in st["fields"].items() if v["page"] == page},
            "transactions": [t for t in st["transactions"] if t["page"] == page],
            "doc_currency": (st["fields"].get("currency") or {}).get("value"),
        }

    def one(task: tuple[dict, int]) -> dict:
        st, page = task
        ident = {"statement_id": st["statement_id"], "page": page}
        if not st.get("page_images"):
            return {**ident, "error": "no page image (generate with --png)"}
        image = set_dir / st["page_images"][page - 1]["file"]
        try:
            reading, meta = read_page(client, model, image, label=f"eval:{st['statement_id']}:p{page}")
        except (NotRecorded, RuntimeError) as e:  # an API error fails this page, not the whole run
            return {**ident, "error": str(e)}
        snapped = snap_rows(reading, image)
        return {
            **score_page(page_view(st, page), reading, snapped, snap_fields(reading, image)),
            **ident,
            "call": meta,
            "reading": reading,
        }

    tasks = [(st, p) for st in statements for p in range(1, (st.get("pages") or 1) + 1)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pages = list(pool.map(one, tasks))
    scored = [p for p in pages if "error" not in p]
    return {
        "model": model,
        "prompt": PROMPT_VERSION,
        "set": str(set_dir),
        "mode": mode,
        "generator": truth["generator"],
        "cost_usd_this_run": round(
            sum(p["call"]["cost_usd"] for p in scored if not p["call"]["replayed"]), 4
        ),
        "errors": [p for p in pages if "error" in p],
        "summary": summarize(scored) if scored else {},
        "pages": pages,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--set", type=Path, default=Path("out/synth"))
    p.add_argument("--mode", choices=["replay", "live", "auto"], default="replay")
    p.add_argument("--model", default="muse-spark-1.3")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int)
    a = p.parse_args()
    result = evaluate(a.set, a.model, a.mode, a.workers, a.limit)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{a.model}_{a.set.name}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("model", "prompt", "mode", "cost_usd_this_run")}))
    print(json.dumps(result["summary"], indent=1))
    for e in result["errors"]:
        print("ERROR", e["statement_id"], e["error"])
    print(f"wrote {out}")
    if result["errors"] or not result["summary"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
