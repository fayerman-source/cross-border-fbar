# Extraction accuracy

How well a Meta model reads synthetic bank statements, scored against the generator's answer key. Raw counts, errors included.

```bash
uv run python -m cross_border_fbar.synth --out out/synth --png
uv run python -m cross_border_fbar.eval --set out/synth            # replay: no key, no network
uv run python -m cross_border_fbar.eval --set out/synth --mode live # calls the API under the spend cap
```

Replay uses the model responses recorded in `recordings/`, keyed by a hash of the exact request (model, prompt, schema, page image). CI runs it on every push.

## Method

- **Input:** each statement page rendered at 150 dpi as a PNG. The model sees only the image and the prompt in `src/cross_border_fbar/extract/prompts/`.
- **The model copies; code computes.** The model returns printed text and boxes on a 0-1000 page grid. Plain code parses amounts (`1.234,56`, `1,234.56`, `1,234`) and dates, including Buddhist-era years.
- **Scoring:**
  - Rows are matched in printed order.
  - A value is correct only if it equals the answer key exactly after parsing.
  - A box is "in the right row" if its centre falls inside the true row.
  - Box overlap (IoU) is measured against the tight box of the printed text.
- **Snapping:** the model's row boxes drift upward down the page. `extract/snap.py` finds printed lines from the image alone (no answer key) and aligns rows to them in order. It abstains when the best alignment doesn't clearly beat the runner-up.

## Results: muse-spark-1.3, clean renders (Oct 27, 2026)

47 pages (Korean 23, German 12 in two layouts, Thai 12), one run. Cost $0.63; median latency 10 s per page.

| Measure | Raw counts |
|---|---|
| Fields: account number, holder, opening, closing balance | 188/188 |
| Transaction rows returned | 240/240 (0 extra) |
| Dates / amounts / running balances | 240/240 / 240/240 / 240/240 |
| Row box in the right row: raw → snapped | 209/240 → 239/240 |
| Balance box IoU ≥ 0.5: raw → snapped | 78/212 → 209/212 |
| Field box centred in its cell (fields aren't snapped yet) | 157/188 |

Full per-page output: `results/muse-spark-1.3_synth.json`.

## What these numbers do not show

- **Clean renders only.** Photographed and scanned copies are the real test and are not in this table yet.
- **The alignment rule was chosen after looking at 3 of the 47 pages.** The other 44 are out of sample for it.
- **Known miss: a page with a single transaction row.** There's no pattern to fit, so the snap can pick the wrong line.
- **One model and one run.** Glimmer (open weights) has not been evaluated yet.
- **No unreadable or missing fields yet,** so abstention isn't scored.
