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
