# cross-border-fbar

A local preparer prototype that turns readings of fictional foreign bank statements into an FBAR review worksheet, with figures linked to source pages and lines.

**Status:** early prototype; synthetic data only. The worksheet opens prepared, recorded readings; a command-line runner can also read a new synthetic image or PDF page with Meta Muse Spark and add it to a review set. There is no browser upload. New capture locations are unverified and shown without highlights.

**Capture test:** one flatbed scan and one WhatsApp-forwarded photo for each of three synthetic pages. Each condition scored 17/18 header fields and returned 19/19 rows; the one mismatch was the Thai institution name. The same source layouts appeared in the clean evaluation, and capture box placement was not scored. See [EVAL.md](EVAL.md) for per-language counts, costs, and limits.

**Scope:** balances, thresholds and review flags only. It does not give tax advice or say whether anyone must file. The separate Form 8938 panel is indicative and not independently validated.

## Built during the event

This repository was created on October 27, 2026, after the hackathon kickoff. The synthetic statement generator, model-reading and evaluation pipeline, deterministic reconciliation, review worksheet, audit/sign-off, Korean-summary workflow, indicative Form 8938 panel, and single-page photo-evaluation runner were built during the event. The project uses new work in this repository; see commit history for dates.

Not yet complete: broader physical-capture and unseen-layout testing; native-speaker review of the full Korean phrase catalog; a browser upload flow; voice intake; and the submission video. Fifteen of 27 Korean phrases have been reviewed; 12 remain unreviewed, including terminology changes and one potentially truncated transcription to confirm. The Korean draft requires explicit preparer approval.

## License and model attribution

The project's original code is available under [Apache-2.0](LICENSE). Bundled fonts retain their separate SIL Open Font License terms; dependencies have their own licenses, including LGPL-3.0 for fpdf2. See [third-party notices](docs/third-party.md). Meta's [Muse Glimmer 30B GGUF model](https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF) is published by Meta under Apache-2.0; model weights are not included in this repository. Muse Spark is accessed through the Meta Model API under its applicable terms.

## Synthetic test data

```bash
uv sync
uv run python -m cross_border_fbar.synth --out out/synth --png
```

This generates a year of monthly statements for one fictional household (Korean, German and Thai banks, all invented) and `truth.json`, the answer key the accuracy test scores against. Each account carries a case the tool must handle:

| Account | Case |
|---|---|
| Korean checking | A deposit passes through for two days in March; no month-end balance shows the peak |
| Parents' Korean account | Signature authority only; the August statement is withheld, and August holds the true peak |
| German joint account | The bank merges mid-year (same IBAN, new layout); the first layout prints no running balance |
| Thai savings | Buddhist-era dates; a same-day pair posted debit-first hides a possible intraday peak |

The answer key follows FinCEN's and the IRS's rules: the maximum is the largest balance appearing on any statement, converted at the Treasury Dec 31 rate and rounded up. Every field and line also carries its position on the page (0-1000 grid), so extracted locations can be scored.

## The review worksheet

```bash
uv run python -m cross_border_fbar.app --set out/synth --readings results/muse-spark-1.3_synth.json
# open http://127.0.0.1:8765
```

A local app for the preparer. The recorded-reading worksheet runs offline, binds to localhost, and needs no build step. A fresh model translation for missing catalog phrases may require API access.

- **Schedule A** lists each account's highest balance with a tickmark and a workpaper reference. Click a figure to see the source page with the line circled.
- **Schedule B** lists the review items. Each must be acknowledged with a note; ownership (owner, joint, signature authority) can be corrected, and the worksheet recomputes.
- **Sign-off** is allowed only when every review item is acknowledged. It freezes the worksheet and records a fingerprint; export exists only after it.
- **Client summary in Korean** is assembled from signed figures and fixed catalog phrases (last four digits, no statement contents, none of the preparer's notes). Missing phrases may be translated by `muse-spark-1.3`; unreviewed phrases require explicit confirmation and the preparer approves the draft before release. Native-speaker review is partial, with one potentially truncated transcription awaiting confirmation.

Every action goes to an audit trail (`out/review/audit.jsonl`). The app reads the model's recorded page readings and the intake facts, never the answer key.


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
