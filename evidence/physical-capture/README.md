# Physical capture test: the evidence

Three synthetic statement pages were printed, then captured once by a 300-dpi flatbed scanner and once by a phone. The phone photos were recompressed by WhatsApp. Every page is fictional and says so in its footer.

Each folder holds what one live `muse-spark-1.3` call received and returned:

| File | What it is |
|---|---|
| `page-sent-to-model.png` | The exact image sent to the model |
| `readings.json` | The model's response, its call details and the score recorded at the time |
| `photo-as-received.jpeg` | The photo as WhatsApp delivered it, in the `-whatsapp` folders only |

The original scans are 32 MB TIFFs and aren't included; each scan's PNG here is the image the model received.

`tests/test_physical_evidence.py` rescores these readings against the current answer key on every CI run, and checks that each photo matches the hash recorded when it was read:

```bash
uv run python -m pytest tests/test_physical_evidence.py
```

See [EVAL.md](../../EVAL.md#physical-capture-test-muse-spark-oct-30-2026) for the results and their limits.
