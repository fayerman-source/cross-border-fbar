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
  - A bank name counts if it is one or more of the names the page prints and nothing else. The renderer records those names as it draws them, so `Saebyeol Bank` is not accepted on a Korean page that doesn't print it. See [the answer-key change](#answer-key-change-nov-1-2026).
  - A box is "in the right row" if its centre falls inside the true row.
  - Box overlap (IoU) is measured against the tight box of the printed text.
- **Snapping:** the model's row boxes drift vertically, by different amounts on different runs. `extract/snap.py` finds printed lines in the image alone, with no answer key. Its primary signal is the model's horizontal positions, which stay accurate. Each row's amount sits in a debit or credit column, so its true line has ink in that column and none in the other. When that pattern can't decide (one-column layouts), a fit-plus-anchor cost is used, and the code abstains rather than guess. Header fields such as an opening balance are placed separately: the printed line must fill about the same columns as the model's box and lie within one line of it, and if two lines fit equally well, no highlight is drawn.
- **Reviewer workload:** every actual review-severity flag is counted, including multiple items of the same type and flags on unmatched accounts. Each expected review item matches at most one actual flag for the same account, code, and month; unmatched actual review items are additional workload. Two subsets are reported separately and are already in that count: items raised because the reader chose different printed bank names across pages, and items linked to scored header-field misreads. The misread link covers mapped institution/currency flags and unreadable header fields; other items may still arise from misreads. Additional review items are not necessarily false positives. Matching by code and month cannot distinguish multiple causes represented by one flag, so this counts items on this test set and does not estimate time or cost.
- **Reconciliation scoring:**
  - Per account: the maximum, the statement it came from, and its USD value. A value counts only if it matches the answer key exactly.
  - Review and info flags are scored as true positives, false positives and misses, deduplicated by account, code and month. A currency flag linked to a header misread is reported separately from false positives. A bank-name flag the key doesn't expect is reported separately only when every name read for that account is printed on the page it was read from. Any other name counts as a false positive and fails the CI gate.
  - Intake facts (owner vs signature authority) come from the answer key here. In the product they come from the client call or the preparer.

## Results: muse-spark-1.3, prompt v3, clean renders (Oct 27, 2026)

**The set:** 49 pages.
- 47 monthly statements: Korean 23, German 12 in two layouts, Thai 12.
- One 2-page compiled transaction list covering several accounts, with no balances. Its layout is modeled on a real Korean compiled list; its content is entirely synthetic.

One recorded run per page; median latency is about 10 s per page. The 49 per-page recorded costs in the current replay sum to **$0.87**.

### Extraction

| Measure | Raw counts |
|---|---|
| Fields: bank, currency, account number, holder, opening, closing (or correctly reported absent) | 294/294 |
| Transaction rows returned | 280/280 (0 extra) |
| Dates / amounts / running balances | 280/280 / 280/280 / 280/280 |
| Per-row account numbers (compiled list, including a masked one) | 280/280 |
| Row box in the right row: raw → snapped | 177/280 → 278/280 |
| Balance box IoU ≥ 0.5: raw → snapped | 33/212 → 182/212 |
| Header field box in the right place: raw → snapped | 237/283 → 262/283 (2 wrong, 19 with no highlight) |
| Opening and closing balance boxes, snapped | 84/94 (0 wrong, 10 with no highlight) |

Two of the 280 rows don't count as snapped:
- **One is a single-row Thai page.** One row gives no pattern to align, so snapping abstains, and the raw box was off.
- **The other is a row on an abstaining page,** which counts as not snapped even when its raw box was right.

### Reconciliation: model readings → FBAR worksheet

| Measure | Result |
|---|---|
| Accounts found | 6/6 |
| Maximum / source / USD, including two whose correct answer is "unknown" | 6/6 / 6/6 / 6/6 |
| Flags | 9 correct, 0 false, 0 missed |
| Review workload | 6 actual review items; 0 additional |
| Household aggregate | $373,618 (answer key: $373,618) |

The two "unknown" accounts are the point of the compiled list:
- **An account that appears only as transactions** gets no maximum and a `no_balance_anchor` review flag asking for statements with balances.
- **Rows under a partly masked account number** are flagged `account_number_masked` and kept out of the totals.

## Open weights: Muse Glimmer on a consumer GPU (Oct 28, 2026)

Meta's open-weights model read the same 49 pages with the same prompt v3, on the GPU host over an SSH tunnel. There was no third-party inference endpoint and no per-token model charge. The data are synthetic.

**Runtime:**
- llama.cpp b11941 (commit 19ff817fb), CUDA build.
- Model `meta-models/Muse-Glimmer-30B-GGUF`, file `Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf`.
- Vision projector `mmproj-Muse-Glimmer-30B-Q4_K_M.gguf`.
- Hardware: an NVIDIA RTX 5070 (12 GB) laptop GPU under WSL2.
- Settings: partial GPU offload (`--fit on --fit-target 5500`, 14 CPU threads), 16k context, `reasoning_strength` low.
- Reached through an SSH tunnel; recorded responses are in `recordings/muse-glimmer/`.

| Measure | Muse Glimmer (self-hosted) | muse-spark-1.3 (hosted) |
|---|---|---|
| Dates / amounts / balances / row accounts | 280/280 each | 280/280 each |
| Header fields (strict: status must be "read") | 244/294 | 294/294 |
| Row box in the right row, raw | 211/280 | 177/280 |
| Row box in the right row, snapped | 244/280 | 278/280 |
| Header field box in the right place, raw | 270/283 | 237/283 |
| Header field box in the right place, snapped | 275/283 (1 wrong, 7 with no highlight) | 262/283 (2 wrong, 19 with no highlight) |
| Opening and closing balance boxes, snapped | 93/94 (0 wrong, 1 with no highlight) | 84/94 (0 wrong, 10 with no highlight) |
| Reconciliation: maximum / USD | 6/6 / 6/6 | 6/6 / 6/6 |
| Reconciliation: flags | 9 correct, 0 false, 0 missed, 1 raised by a printed-name choice | 9 correct, 0 false, 0 missed |
| Actual review workload | 7 items: 6 expected, 1 additional from a printed-name choice | 6 items: 6 expected, 0 additional |
| Household aggregate | $373,618 (key: $373,618) | $373,618 |
| Median time per page | 344 s | ~10 s |
| Cost | $0 model-token charge (4.9 h on the GPU; hardware and electricity excluded) | $0.87 recorded model charges |

What the gaps are:
- **Header fields:** on 8 pages Glimmer returned the right header text but labeled every field `absent`. The strict field score counts those as misses. The reconciliation now uses text that is present whatever its label, which recovered the end-to-end result. The other two misses are on the compiled list: Glimmer returned one account number as the page header, but the list covers several accounts and prints none there.
- **Thai bank name:** on two statements it read the English name, or both names, instead of the Thai one; both are printed. The tool doesn't decide from the text whether two names are one bank, so it raises a review flag listing each name, its months and a page for it. The flag costs the reviewer one item and stays in the workload count. The eval reports it as raised by a printed-name choice because every name on its source pages is printed there. An earlier version matched names by script, then by containment; each rule either merged two different banks or split one bank in two.
- **Boxes on the compiled list:** only 5/40 rows, raw and snapped. The single in/out column gives no column pattern, and the fallback's anchor rule doesn't fit Glimmer's drift on that layout. Values on those rows are all correct; only the highlighted line would be off. This is open work.

## Answer-key change (Nov 1, 2026)

The answer key was changed **after seeing these results**, so the counts below are not blind. Thai statements print both `ธนาคารสยามโลตัส` and `Siam Lotus Bank`. The key listed only the Thai name, so returning the English one scored as a miss even though it is a correct read of the page. The key now lists every name each page prints, and the scorer accepts one or more of them with nothing added. Empty text and any other name still fail. The page images did not change: regenerated PNGs are byte-identical, so the recordings replay unchanged and no model was called.

| Measure, same recorded readings | Before | After |
|---|---:|---:|
| muse-spark-1.3 clean set: header fields | 294/294 | 294/294 |
| Muse Glimmer clean set: header fields | 243/294 | 244/294 |
| Muse Glimmer: Thai pages, header fields | 65/72 | 66/72 |
| Muse Glimmer: flags raised by a misread | 1 | 0 |
| Muse Glimmer: flags raised by a printed-name choice | not counted | 1 |
| Muse Glimmer: false-positive flags | 0 | 0 |
| Muse Glimmer: review items | 7 | 7 |
| Physical capture, flatbed scans: header values | 17/18 | 18/18 |
| Physical capture, WhatsApp-forwarded photos: header values | 17/18 | 18/18 |

Glimmer's `th-savings` review flag did not go away. It is still raised, still listed for the reviewer and still counted in the workload. It moved from "raised by a misread" to its own count because the pages it cites do print the names it reports. If any name read for that account were not printed on its page, the flag would count as a false positive and fail the gate.

The physical counts were rescored from the saved readings of the Oct 30 captures, with no new model call. The images and readings are in [evidence/physical-capture](evidence/physical-capture), and `tests/test_physical_evidence.py` rescores them on every CI run.

## Run-to-run variance in boxes

Box placement changes from run to run, and snapping has had to change with it:
- **Run 1 (prompt v1):** the first-row-anchor rule. 209/240 raw rows in the right row.
- **Run 2:** 187/240 raw. The anchor rule snapped one page confidently onto the wrong lines; the column-pattern rule replaced it.
- **Run 3 (prompt v3):** the model compressed row spacing on one German page by 16%. The fallback path's ±15% scale guard excluded the right alignment, so it snapped confidently wrong again. The guard is now ±35%, and the fallback must also sit near the model's rows.

The current clean-run snapped result is 278/280; snapping was revised after inspecting failures and is in-sample. The header-field rule's thresholds were also chosen on these same pages. The capture test below did not score box placement.

## Physical capture test: Muse Spark (Oct 30, 2026)

One live Standard-mode call per capture, with no retries or tuning against this set. Three synthetic statement pages were each printed and captured once by a 300-dpi flatbed scanner and once by a phone. The phone images were recompressed by WhatsApp before testing, and the Thai photo was sideways. These are the same underlying pages and layouts used in the clean evaluation, so this tests capture robustness, not generalization to unseen layouts.

| Language | Condition | Header values: original key → current key | Rows returned | Dates | Amounts | Balances | Row accounts | Extra rows | Unreadable header fields | Review items | Cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Korean | Flatbed scan | 6/6 → 6/6 | 12/12 | 12/12 | 12/12 | 12/12 | 12/12 | 0 | 0/7 | 13 | $0.03 |
| Korean | WhatsApp-forwarded photo | 6/6 → 6/6 | 12/12 | 12/12 | 12/12 | 12/12 | 12/12 | 0 | 0/7 | 13 | $0.02 |
| German | Flatbed scan | 6/6 → 6/6 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 0 | 0/7 | 13 | $0.03 |
| German | WhatsApp-forwarded photo | 6/6 → 6/6 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 0 | 0/7 | 13 | $0.03 |
| Thai | Flatbed scan | 5/6 → 6/6 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 0 | 0/7 | 14 | $0.02 |
| Thai | WhatsApp-forwarded photo | 5/6 → 6/6 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | 0 | 0/7 | 14 | $0.04 |
| **All scans** | Flatbed scan | **17/18 → 18/18** | **19/19** | **19/19** | **19/19** | **19/19** | **19/19** | **0** | **0/21** | **40** | **$0.07** |
| **All photos** | WhatsApp-forwarded photo | **17/18 → 18/18** | **19/19** | **19/19** | **19/19** | **19/19** | **19/19** | **0** | **0/21** | **40** | **$0.10** |

Total calculated token cost was **$0.17** (about $0.03 per page), not independently reconciled to a provider invoice. Individual and aggregate amounts are rounded independently to cents, so displayed rows may not sum to the displayed total. The six-field header score covers bank, currency, account number, holder, opening and closing balances. Period is separate: all six parsed to the printed period. The row schema has no explicit unreadable status; the abstention counts above cover seven header statuses per capture. Row descriptions were not scored.

**Thai institution:** both captures returned `Siam Lotus Bank`. The original key listed only `ธนาคารสยามโลตัส` and scored both as misses; no scorer change was made for the test itself. The English name is printed on the page, so the current key counts it as correct: see [the answer-key change](#answer-key-change-nov-1-2026), which was made after these results were seen.

Each single-page review set raised 11 missing-month items, one unknown-ownership item and one unverified-location item; each Thai capture also raised a possible intraday-peak item. These are actual review-severity items, not a measured false-positive count. No full-year reconciliation accuracy claim follows from a single page.

No page corners were supplied: photo **and scan** box placement was not scored, and source boxes remain unverified in the worksheet. `python -m cross_border_fbar.eval.photo` can optionally rectify using operator-supplied corners, but that geometry requires manual alignment verification. See the README for the command.

## What these numbers do not show

- **Small physical set.** Only three previously used synthetic pages were tested, each as a scan and a WhatsApp-forwarded photo. No new layouts were tested, and capture box placement was not scored.
- **Snapping was revised after seeing failures.** The current clean-run result of 278/280 and the header-field counts are in-sample.
- **One recorded run per model on the current clean set.** These are not repeated-run reliability estimates.
- **No intentionally unreadable document fields in the clean set.** Expected absent fields are scored; abstention on unreadable input is not yet established.
- **Same author.** The answer key and the reconciliation were written separately but by the same author, so a shared misunderstanding of the rules would not show up here.
