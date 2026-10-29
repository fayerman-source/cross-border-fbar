Flag definitions live in `src/cross_border_fbar/reconcile/flags.py`. The catalog supplies severity, the triggering condition, CPA actions, worksheet titles and client labels. Unknown codes fail at construction; the catalog test checks emitted codes in both directions.

Currency conflicts withhold the native maximum, dollar maximum and year-end balance. The CPA may correct the currency label for all statements only by choosing an observed currency and recording the supporting source note. This correction does not convert genuinely mixed currencies into one denomination: those documents need correction before calculating a single account value. The selection and note are audited and included in the signed worksheet.

When Treasury has no rate, the acknowledgement dialog accepts foreign currency units per USD, a verifiable source and a rate date. The date must be the last day of the tax year. Leaving the rate blank leaves the dollar amount undetermined. Recorded rates recompute the worksheet, appear next to the account and are included in its fingerprint. Reopening that review item removes its rate. Changing account figures requires acknowledgement of that account's flags again.

The Korean gettext catalog is `src/cross_border_fbar/app/translations/ko.po`. Each stable message context names a label, with a translator comment identifying its grammar. English changes retain the old translation as fuzzy. A translation is reviewed only when `Last-Translator` names the human reviewer, the file header is not fuzzy and the matching entry is not fuzzy. Current fuzzy drafts can be used only with explicit CPA confirmation at approval. Stale English is never used as a reviewed translation.

Give `docs/korean-review.txt` to the native reviewer. It contains numbered English | Korean pairs and review status. After incorporating the review into the `.po` file, record the human's name in `Last-Translator` and remove fuzzy only for approved entries. Regenerate the printout without an API call:

```
uv run python -m cross_border_fbar.app.phrases --printout
```

`--refresh` deliberately records a new translation of all phrases in one call and resets their review status. Terminology follows IRS Publication 850 (EN-KR), September 2023, where terms exist. The current draft awaits the founder's native reviewer; no human review is claimed.
