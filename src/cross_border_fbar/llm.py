"""Meta Model API client: Standard mode only, a local spend cap, and record/replay.

- Only Standard-mode model IDs are allowed; any "-contributor" ID is refused
  before a request is built (Contributor mode may train on inputs).
- Every call is priced before it is sent, assuming every allowed output token is
  used, and refused if it could push the shared ledger past the cap. Admission
  is atomic: under a file lock, the call reserves its worst case in the ledger,
  so concurrent threads and processes see each other's in-flight spend. A failed
  call keeps its reservation because the provider may still bill an unknown amount.
- Responses are recorded under recordings/, keyed by a hash of the exact
  request, so the eval re-runs offline and without a key ("replay").

The key comes from MODEL_API_KEY or ~/.config/fbar/.env and is never logged.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

BASE_URL = "https://api.meta.ai/v1"
CONFIG_DIR = Path.home() / ".config" / "fbar"
RECORDINGS = Path(__file__).resolve().parents[2] / "recordings"
MAX_SPEND_USD = 50.0

# USD per 1M tokens, Standard mode (https://dev.meta.ai/docs/pricing-rate-limits, read 2026-10-26).
PRICES = {
    "muse-spark-1.3": {"input": 1.25, "cached": 0.15, "output": 4.25},
    "muse-wonder-1.0": {"input": 4.00, "cached": 0.20, "output": 20.00},
    # Meta open weights on a GPU host reached through an SSH tunnel: no per-token model charge.
    "muse-glimmer": {"input": 0.0, "cached": 0.0, "output": 0.0},
}
# Self-hosted models are served by llama.cpp (OpenAI-compatible); reached through an SSH tunnel by default.
SELF_HOSTED = {"muse-glimmer"}
SELF_HOSTED_URL = os.environ.get("FBAR_SELF_HOSTED_URL", "http://127.0.0.1:8080/v1")


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


def _validated_usage(data: dict) -> dict:
    """Do not release a paid call's reservation without reliable token counts."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("missing or invalid token usage; spend reservation retained")
    prompt, output = usage.get("prompt_tokens"), usage.get("completion_tokens")
    details = usage.get("prompt_tokens_details")
    if details is None:
        details = {}
    if not isinstance(details, dict):
        raise ValueError("invalid token usage details; spend reservation retained")
    cached = details.get("cached_tokens", 0)
    if any(type(n) is not int or n < 0 for n in (prompt, output, cached)) or cached > prompt:
        raise ValueError("invalid token counts; spend reservation retained")
    return usage


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
        self.cap_usd = (
            cap_usd
            if cap_usd is not None
            else float(os.environ.get("FBAR_SPEND_CAP_USD", str(MAX_SPEND_USD)))
        )
        if not math.isfinite(self.cap_usd) or not 0 <= self.cap_usd <= MAX_SPEND_USD:
            raise ValueError(f"spend cap must be between $0 and ${MAX_SPEND_USD:.2f}")
        self.ledger = ledger or Path(os.environ.get("FBAR_SPEND_LEDGER", CONFIG_DIR / "spend_ledger.jsonl"))
        self.recordings = recordings

    def spent_usd(self) -> float:
        """Recorded spend plus worst-case reservations for calls still in flight."""
        if not self.ledger.exists():
            return 0.0
        return sum(json.loads(line)["cost_usd"] for line in self.ledger.read_text().splitlines() if line)

    @contextlib.contextmanager
    def _ledger_lock(self):
        # One lock file per ledger, so threads and separate processes admit calls one at a time.
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger.with_suffix(".lock"), "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _append(self, entry: dict) -> None:
        with self.ledger.open("a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}) + "\n")

    def _reserve(self, worst: float, label: str) -> str:
        """Admit a call atomically by reserving its worst-case cost; raise if that would pass the cap."""
        rid = uuid.uuid4().hex
        with self._ledger_lock():
            already = self.spent_usd()
            if already + worst > self.cap_usd:
                raise SpendCapExceeded(
                    f"spent ${already:.4f} + worst case ${worst:.4f} > cap ${self.cap_usd:.2f}"
                )
            self._append({"kind": "reservation", "id": rid, "label": label, "cost_usd": worst})
        return rid

    def _settle(self, rid: str, worst: float, actual: dict | None) -> None:
        """Release a reservation and, if the call completed, record its actual cost."""
        with self._ledger_lock():
            self._append({"kind": "release", "id": rid, "cost_usd": -worst})
            if actual:
                self._append(actual)

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
        worst = round((est_input * PRICES[model]["input"] + max_tokens * PRICES[model]["output"]) / 1e6, 6)
        rid = self._reserve(worst, label)
        # Failures retain the reservation: an interrupted request may still be billed.
        started = time.time()
        data = self._post(body)
        latency = time.time() - started
        usage = _validated_usage(data)
        cost = cost_usd(model, usage)
        content = data["choices"][0]["message"]["content"]
        self._settle(
            rid,
            worst,
            {
                "label": label,
                "model": model,
                "usage": usage,
                "cost_usd": round(cost, 6),
                "latency_s": round(latency, 2),
            },
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
        if body["model"] in SELF_HOSTED:  # the Meta API key never goes to the self-hosted server
            url, headers = f"{SELF_HOSTED_URL}/chat/completions", {"Content-Type": "application/json"}
        else:
            url = f"{BASE_URL}/chat/completions"
            headers = {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:  # partial GPU offload is slow
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
