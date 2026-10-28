# Accuracy

How well a Meta model reads synthetic bank statements, and whether the numbers reconciled from those readings match the answer key. Raw counts, errors included.

```bash
uv run python -m cross_border_fbar.synth --out out/synth --png
uv run python -m cross_border_fbar.eval --set out/synth                 # extraction, replayed
uv run python -m cross_border_fbar.eval.reconcile_eval --set out/synth  # reconciliation
uv run python -m cross_border_fbar.eval --set out/synth --mode live     # re-read with the API (spend-capped)
```

Replay uses the model responses recorded in `recordings/`, keyed by a hash of the exact request (model, prompt, schema, page image), so it needs no key and no network. CI runs both evals on every push and fails on any regression.

## Method

- **Input:** each statement page rendered at 150 dpi as a PNG. The model sees only the image and the versioned prompt in `src/cross_border_fbar/extract/prompts/`.
- **The model copies; code computes.** The model returns printed text and boxes on a 0-1000 page grid. Plain code parses amounts (`1.234,56`, `1,234.56`, `1,234`), dates (including Buddhist-era years), statement periods and currencies, then reconciles.
- **Extraction scoring:**
  - Rows are matched in printed order.
  - A value counts only if it equals the answer key exactly after parsing.
  - A box is "in the right row" if its centre falls inside the true row.
  - Box overlap (IoU) is measured against the tight box of the printed text.
- **Snapping:** the model's row boxes drift vertically, by different amounts on different runs. `extract/snap.py` finds printed lines in the image alone, with no answer key. Its primary signal is the model's horizontal positions, which stay accurate: each row's amount sits in a debit or credit column, so its true line has ink in that column and none in the other. When that pattern can't decide (one-column layouts), a fit-plus-anchor cost is used, and the code abstains rather than guess.
- **Reconciliation scoring:**
  - Per account: the maximum, the statement it came from, and its USD value. A value counts only if it matches the answer key exactly.
  - Review and info flags are scored as true positives, false positives and misses.
  - Intake facts (owner vs signature authority) come from the answer key here. In the product they come from the client call or the CPA.

## Results: muse-spark-1.3, prompt v3, clean renders (Oct 27, 2026)

**The set:** 49 pages.
- 47 monthly statements: Korean 23, German 12 in two layouts, Thai 12.
- One 2-page compiled transaction list covering several accounts, with no balances. Its layout is modeled on a real Korean compiled list; its content is entirely synthetic.

One run. Recording cost $0.45 for the 26 pages not already recorded; median latency is about 10 s per page.

### Extraction

| Measure | Raw counts |
|---|---|
| Fields: bank, currency, account number, holder, opening, closing (or correctly reported absent) | 294/294 |
| Transaction rows returned | 280/280 (0 extra) |
| Dates / amounts / running balances | 280/280 / 280/280 / 280/280 |
| Per-row account numbers (compiled list, including a masked one) | 280/280 |
| Row box in the right row: raw → snapped | 177/280 → 278/280 |
| Balance box IoU ≥ 0.5: raw → snapped | 33/212 → 182/212 |

Two of the 280 rows don't count as snapped:
- **One is a single-row Thai page.** One row gives no pattern to align, so snapping abstains, and the raw box was off.
- **The other is a row on an abstaining page,** which counts as not snapped even when its raw box was right.

### Reconciliation: model readings → FBAR worksheet

| Measure | Result |
|---|---|
| Accounts found | 6/6 |
| Maximum / source / USD, including two whose correct answer is "unknown" | 6/6 / 6/6 / 6/6 |
| Flags | 9 correct, 0 false, 0 missed |
| Household aggregate | $373,618 (answer key: $373,618) |

The two "unknown" accounts are the point of the compiled list:
- **An account that appears only as transactions** gets no maximum and a `no_balance_anchor` review flag asking for statements with balances.
- **Rows under a partly masked account number** are flagged `account_number_masked` and kept out of the totals.

## Run-to-run variance in boxes

Box placement changes from run to run, and snapping has had to change with it:
- **Run 1 (prompt v1):** the first-row-anchor rule. 209/240 raw rows in the right row.
- **Run 2:** 187/240 raw. The anchor rule snapped one page confidently onto the wrong lines; the column-pattern rule replaced it.
- **Run 3 (prompt v3):** the model compressed row spacing on one German page by 16%. The fallback path's ±15% scale guard excluded the right alignment, so it snapped confidently wrong again. The guard is now ±35%, and the fallback must also sit near the model's rows.

Earlier detail: an earlier run (prompt v1, before pages printed a currency line) placed 209/240 raw row boxes in the right row; this run placed 187/240. The direction and size of the drift differed between runs. The first snapping rule assumed the first row drifts least. It reached 239/240 on run 1 but snapped one page confidently onto the wrong lines on run 2 (218/240). The column-pattern rule above reaches 240/240 on run 2. Treat box placement as variable, and judge snapping on data it wasn't tuned on. The photographed and scanned copies are that test.

## What these numbers do not show

- **Clean renders only.** Photographed and scanned copies are the real test and are not in these tables yet.
- **Snapping was revised after seeing this run's failures.** Its 240/240 is in-sample.
- **One model, one run.** Glimmer (open weights) has not been evaluated.
- **No unreadable or missing fields on the pages,** so abstention isn't scored yet.
- **Same author.** The answer key and the reconciliation were written separately but by the same author, so a shared misunderstanding of the rules would not show up here.
