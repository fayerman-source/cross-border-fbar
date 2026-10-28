# cross-border-fbar

Foreign bank statements in, an FBAR worksheet out, with every number linked to the statement line it came from.

Built during the Meta Global AI Developer Hackathon (Oct 26 - Nov 4, 2026). This repository was created on Oct 27, 2026 and contains only work done during the event.

**Status:** early build. Synthetic data only; no real person's statements are used anywhere.

**Scope:** balances, thresholds and review flags only. It does not give tax advice or say whether anyone must file.

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

A local app for the preparer. It runs offline, binds to localhost, and needs no build step.

- **Schedule A** lists each account's highest balance with a tickmark and a workpaper reference. Click a figure to see the source page with the line circled.
- **Schedule B** lists the review items. Each must be acknowledged with a note; ownership (owner, joint, signature authority) can be corrected, and the worksheet recomputes.
- **Sign-off** is allowed only when every review item is acknowledged. It freezes the worksheet and records a fingerprint; export exists only after it.
- **Client summary in Korean** is drafted by `muse-spark-1.3` from the signed figures only (last four digits, no statement contents, none of the preparer's notes). It is checked against the scope line, and released only when the preparer approves it.

Every action goes to an audit trail (`out/review/audit.jsonl`). The app reads the model's recorded page readings and the intake facts, never the answer key.
