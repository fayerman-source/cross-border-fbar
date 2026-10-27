"""Meta Model API client: Standard mode only, a local spend cap, and record/replay.

- Only Standard-mode model IDs are allowed; any "-contributor" ID is refused
  before a request is built (Contributor mode may train on inputs).
- Every call is priced before it is sent, assuming every allowed output token is
  used, and refused if it could push the shared ledger past the cap.
- Responses are recorded under recordings/, keyed by a hash of the exact
  request, so the eval re-runs offline and without a key ("replay").

The key comes from MODEL_API_KEY or ~/.config/fbar/.env and is never logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BASE_URL = "https://api.meta.ai/v1"
CONFIG_DIR = Path.home() / ".config" / "fbar"
RECORDINGS = Path(__file__).resolve().parents[2] / "recordings"

# USD per 1M tokens, Standard mode (https://dev.meta.ai/docs/pricing-rate-limits, read 2026-10-26).
PRICES = {
    "muse-spark-1.3": {"input": 1.25, "cached": 0.15, "output": 4.25},
    "muse-wonder-1.0": {"input": 4.00, "cached": 0.20, "output": 20.00},
}


class SpendCapExceeded(RuntimeError):
    pass


class NotRecorded(LookupError):
    pass


@dataclass
class Result:
    content: str
    usage: dict
    cost_usd: float
    latency_s: float
    replayed: bool
    key: str


def check_model(model: str) -> None:
    if model.endswith("-contributor") or model not in PRICES:
        raise ValueError(f"model {model!r} is not on the Standard-mode allowlist {sorted(PRICES)}")


def request_key(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cost_usd(model: str, usage: dict) -> float:
    p = PRICES[model]
    prompt = usage.get("prompt_tokens", 0)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    output = usage.get("completion_tokens", 0)
    return ((prompt - cached) * p["input"] + cached * p["cached"] + output * p["output"]) / 1e6


class Client:
    def __init__(
        self,
        mode: str = "auto",  # replay | live | auto (replay if recorded, else live)
        cap_usd: float | None = None,
        ledger: Path | None = None,
        recordings: Path = RECORDINGS,
    ):
        if mode not in ("replay", "live", "auto"):
            raise ValueError(mode)
        self.mode = mode
        self.cap_usd = cap_usd if cap_usd is not None else float(os.environ.get("FBAR_SPEND_CAP_USD", "20"))
        self.ledger = ledger or Path(os.environ.get("FBAR_SPEND_LEDGER", CONFIG_DIR / "spend_ledger.jsonl"))
        self.recordings = recordings

    def spent_usd(self) -> float:
        if not self.ledger.exists():
            return 0.0
        return sum(json.loads(line)["cost_usd"] for line in self.ledger.read_text().splitlines() if line)

    def chat(
        self,
        model: str,
        messages: list,
        *,
        max_tokens: int,
        response_format: dict | None = None,
        label: str = "",
    ) -> Result:
        check_model(model)
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if response_format:
            body["response_format"] = response_format
        key = request_key(body)
        rec = self.recordings / model / f"{key}.json"
        if self.mode in ("replay", "auto") and rec.exists():
            r = json.loads(rec.read_text())
            return Result(r["content"], r["usage"], r["cost_usd"], r["latency_s"], True, key)
        if self.mode == "replay":
            raise NotRecorded(f"no recording for {label or key[:12]} under {rec.parent}")

        est_input = len(json.dumps(messages)) // 3  # generous: images count as text here
        worst = (est_input * PRICES[model]["input"] + max_tokens * PRICES[model]["output"]) / 1e6
        already = self.spent_usd()
        if already + worst > self.cap_usd:
            raise SpendCapExceeded(
                f"spent ${already:.4f} + worst case ${worst:.4f} > cap ${self.cap_usd:.2f}"
            )

        started = time.time()
        data = self._post(body)
        latency = time.time() - started
        usage = data.get("usage") or {}
        cost = cost_usd(model, usage)
        content = data["choices"][0]["message"]["content"]
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "label": label,
                        "model": model,
                        "usage": usage,
                        "cost_usd": round(cost, 6),
                        "latency_s": round(latency, 2),
                    }
                )
                + "\n"
            )
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(
            json.dumps(
                {
                    "label": label,
                    "model": model,
                    "content": content,
                    "usage": usage,
                    "cost_usd": round(cost, 6),
                    "latency_s": round(latency, 2),
                    "finish_reason": data["choices"][0].get("finish_reason"),
                },
                ensure_ascii=False,
                indent=1,
            )
        )
        return Result(content, usage, cost, latency, False, key)

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{BASE_URL}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # The response body is safe to surface; the request headers are not.
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from None


def _api_key() -> str:
    if os.environ.get("MODEL_API_KEY"):
        return os.environ["MODEL_API_KEY"]
    env = CONFIG_DIR / ".env"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("MODEL_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("MODEL_API_KEY is not set (env var or ~/.config/fbar/.env)")
