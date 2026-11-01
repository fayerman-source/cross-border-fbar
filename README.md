# cross-border-fbar

**A review tool for US tax preparers (CPAs) with clients abroad. It reads foreign bank statements (tested on Korean, German and Thai), finds each account's highest balance of the year, and links every figure to the page it came from.**

## The problem it solves

Americans whose foreign bank accounts total more than $10,000 report them to the US Treasury each year on a form called the FBAR. The form asks for each account's **highest balance during the year**, not its year-end balance. Preparers can get that wrong: clients send statements in other languages and date formats, sometimes as phone photos, and a brief spike between month ends never appears in a month-end balance.

**Example.** Joy is a fictional US citizen living in Seoul, with accounts in Korea, Germany and Thailand. In March, a 250,000,000-won deposit refund passes through her Korean checking account and leaves two days later, so the balance peaks at about $199,100 and no month-end balance shows it. Month-end balances across her accounts total **$121,350**. The highest balances on her statements total **$373,618**.

## What it does

1. **Read:** a Meta Muse model reads each statement page into structured fields and rows.
2. **Check:** ordinary code, not the model, recomputes the balances line by line and raises review items for missing months, mismatches and possible same-day peaks.
3. **Review:** the CPA sees a worksheet of highest balances. Clicking a figure opens its source page. The CPA acknowledges each review item, corrects facts such as ownership, and signs off.
4. **Tell the client:** a short Korean note is built from phrases reviewed by a native speaker, and the CPA approves it before release.

![Review worksheet showing a highest balance linked to its highlighted source line on a synthetic statement](docs/worksheet.png)

## Try it in about 2 minutes

No API key is needed. Requires [uv](https://docs.astral.sh/uv/getting-started/installation/). All the data is fictional.

```bash
git clone https://github.com/fayerman-source/cross-border-fbar.git
cd cross-border-fbar
uv sync
uv run python -m cross_border_fbar.synth --out out/synth --png
uv run python -m cross_border_fbar.app --set out/synth --readings results/muse-spark-1.3_synth.json
```

Open http://127.0.0.1:8765. You should see six rows and a total of **$373,618**. Click the 287,450,200 KRW figure on the Korean checking row to see the March peak on its source page.

## Results so far

| Test (all synthetic) | Result |
|---|---|
| Hosted Muse Spark, 49 clean pages | 294/294 header fields; 280/280 rows |
| Open-weights Muse Glimmer on a laptop GPU, same pages | 280/280 row values, about 6 minutes a page, nothing sent to a hosted service |
| Reconciliation, either model | Maximum and USD value right on 6/6 accounts; 9 of 9 expected review flags, none false. Glimmer raised one extra item, caused by a misread bank name |
| Printed pages, re-captured as flatbed scans and as WhatsApp photos | 19/19 rows with every date, amount and balance right; 17/18 header fields |

Full numbers and caveats are in [EVAL.md](EVAL.md).

## Limits

- **Early prototype, synthetic data only.** It has not been tried with a practicing CPA, and these tests measure accuracy, not time saved.
- **Replayed readings.** The worksheet opens recorded readings from the live model, so it runs offline. A command-line runner can read a new synthetic image or PDF page live; there is no browser upload.
- **Captures use known layouts.** The scans and photos were of the same layouts as the clean test, so they show the tool copes with how a page was captured, not with unfamiliar banks. Scans and photos show the source page without a line highlight, because locations on them are not verified. The one header miss was the Thai bank name, returned in its printed English form.
- **A misread amount can't be corrected in the app yet.** It can only be inspected against its source page.
- **Scope:** balances, thresholds and review flags only. A below-threshold result stays undetermined when omitted values or relevant review items could change it. It gives no tax advice and doesn't say whether anyone must file; the CPA decides. The Form 8938 panel is indicative and not independently validated.

## Built during the event

This repository was created on October 27, 2026, after the hackathon kickoff. The synthetic statement generator, model-reading and evaluation pipeline, deterministic reconciliation, review worksheet, audit/sign-off, Korean-summary workflow, indicative Form 8938 panel, and single-page photo-evaluation runner were built during the event. The project uses new work in this repository; see commit history for dates.

Not yet complete:

- broader physical-capture and unseen-layout testing
- a browser upload flow
- voice intake
- the submission video

## Synthetic test data

Run the demo from a clone. The recorded readings and results live in the repository, and the demo set is generated there. An installed package includes only the data and fonts needed to generate synthetic statements and load the bundled rates.

```bash
uv sync
uv run python -m cross_border_fbar.synth --out out/synth --png
```

This generates a year of monthly statements for one fictional household (Korean, German and Thai banks, all invented) and `truth.json`, the answer key the accuracy test scores against. Each account carries a case the tool must handle:

| Synthetic account | Review case |
|---|---|
| Korean checking | A deposit passes through in March and creates a peak between month ends |
| Parents' Korean account | Signature authority only; the August statement is withheld |
| German joint account | The bank merges mid-year with the same IBAN and a new layout |
| Thai savings | Buddhist-era dates and a debit-first same-day pair |

The answer key follows the FinCEN instructions' statement-based method: the maximum is the largest balance appearing on any statement, converted at the Treasury Dec 31 rate and rounded up. Every field and line also carries its position on the page (0-1000 grid), so extracted locations can be scored.

## The review worksheet

```bash
uv run python -m cross_border_fbar.app --set out/synth --readings results/muse-spark-1.3_synth.json
# open http://127.0.0.1:8765
```

A local app for the preparer. The recorded-reading worksheet runs offline, binds to localhost, and needs no build step. Only translating a phrase missing from the catalog calls the API.

- **Schedule A** lists each account's highest observed balance in the supplied material, subject to coverage and review. Click a figure to see its source page. For prepared readings, code snaps the model's box to the printed line and proposes a highlight: on the clean set, 278/280 row centers landed in the right row, measured on the same pages the snapping was tuned on. The preparer must verify each highlight against the page; new captures have no highlight while their locations are unverified.
- **Schedule B** lists the review items. Each must be acknowledged with a note; ownership (owner, joint, signature authority) can be corrected, and the worksheet recomputes.
- **Sign-off** is allowed only when every review item is acknowledged. It records a reviewed snapshot and fingerprint; acknowledged limitations may remain. The signed view and export list review items still present and open threshold determinations. Any change to the worksheet invalidates the sign-off.
- **Client summary in Korean** is assembled from signed figures and fixed catalog phrases (last four digits, no statement contents, none of the preparer's notes). A phrase missing from the catalog is translated by `muse-spark-1.3`. Unreviewed phrases require explicit confirmation, and the preparer approves the draft before release. All 27 current catalog phrases have been reviewed by a native speaker; changed English or new phrases require review again.

Corrections cover ownership, financial interest, currency and exchange rate. Every action goes to an audit trail (`out/review/audit.jsonl`). The app reads the model's recorded page readings and the intake facts, never the answer key.

## Evaluate one synthetic capture

The photo runner accepts synthetic PNG, JPEG, WebP, TIFF and HEIC/HEIF images, other images supported by Pillow, or one selected PDF page. The required confirmation applies to every input: never use a real person's statement. Live calls use Standard-mode `muse-spark-1.3`, the shared local ledger and a $50 maximum spend cap; `FBAR_SPEND_CAP_USD` can set a lower cap. Successful calls with valid usage are recorded separately for each run. The cap is a local ledger limit, not a provider billing guarantee. If a live request fails, is interrupted, or returns invalid token counts, its estimated reservation remains in the ledger rather than assuming no charge; the page is not appended.

```bash
uv run python -m cross_border_fbar.eval.photo path/to/synthetic-photo.jpg \
  --confirm-synthetic --set out/photo-review \
  --manifest path/to/manifest.json --case kr-checking_2025-03
uv run python -m cross_border_fbar.app --set out/photo-review \
  --readings out/photo-review/readings.json --review-dir out/photo-review/review
```

`--manifest` and `--case` are optional together. When supplied, the runner reports field and row value counts against the synthetic case. The manifest's answer key must be inside the manifest directory (including after resolving symlinks). Without a manifest the page is added but not scored. `--page` selects a 1-based page from a PDF; `--truth-page` selects the original page in the manifest when it differs. Intake defaults to unknown; `--intake` supplies an optional synthetic intake JSON when starting the set. Choose the command's paths yourself; do not expose the command as a tool accepting untrusted model-generated paths.

Photo and scan page geometry differs from the original. With no verified page corners, box accuracy is **unscored**. To evaluate localization, supply `--corners 'x,y;x,y;x,y;x,y'` in upper-left, upper-right, lower-right, lower-left order in the EXIF-oriented input image's pixel coordinates. The runner rectifies the page to the answer-key dimensions before extraction and scoring; manually verify the alignment before relying on the geometry results. The exact raster sent to the model is saved for the worksheet. New photo locations are marked unverified, shown as a page without a highlight, and generate a preparer review item until independently checked. Recorded replay remains the CI default.

## License and model attribution

The project's original code is available under [Apache-2.0](LICENSE). Bundled fonts retain their separate SIL Open Font License terms; dependencies have their own licenses, including LGPL-3.0 for fpdf2. See [third-party notices](docs/third-party.md). Meta's [Muse Glimmer 30B GGUF model](https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF) is published by Meta under Apache-2.0; model weights are not included in this repository. Muse Spark is accessed through the Meta Model API under its applicable terms.
