"""The CPA's review: acknowledgements, ownership corrections, sign-off, client summary; all audited.

Rules the review enforces:
- Every review flag must be acknowledged, with a note, before sign-off.
- After sign-off the worksheet is frozen: corrections and acknowledgements are refused, and the
  signed fingerprint is recorded. Export and the client summary exist only after sign-off.
- The client-language summary is generated from the signed figures only, checked against the
  scope line, and released only when the CPA approves it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .worksheet import Inputs, build, worksheet_hash

OWNERSHIPS = ("sole", "joint", "signature_authority")
# A client summary describes what was reviewed; it never talks about filing, obligations or penalties.
# Korean is matched by stem so polite inflections (신고하셔야 합니다, 신고 대상입니다) can't slip through.
OUT_OF_SCOPE = re.compile(
    r"신고|제출\s*(?:하|해|의무|대상|필요)|의무|대상자|벌금|과태료|가산세|처벌|면제|납부|"
    r"\b(?:must|required to|need to|obligat\w*)\s+(?:file|report|submit)|\bpenalt|\bfines?\b|\bexempt",
    re.I,
)


# Client-facing descriptions of review items; the model translates these, never the CPA's notes.
CLIENT_LABELS = {
    "no_balance_anchor": "no balance appears on the documents; statements showing balances were requested",
    "missing_statement": "one month's statement is missing",
    "signature_authority_only": "the account is held by someone else and the client can sign on it",
    "possible_intraday_peak": "a same-day deposit and withdrawal were checked against the highest balance",
    "account_number_masked": "rows with a partly masked account number were matched to the client's accounts",
    "arithmetic_mismatch": "a printed balance was checked against the transactions",
    "continuity_break": "the balances between two statements were checked",
    "coverage_gap": "some days of the year are not covered by a statement",
    "institution_names_differ": "the bank name differs between statements and was checked",
    "currency_conflict": "the currency shown on the statements was checked",
    "currency_unknown": "the account's currency was checked against the Treasury exchange rates",
    "ownership_unknown": "who owns the account was confirmed with the client",
    "field_unreadable": "a part of a statement that couldn't be read was checked",
    "no_statements": "no statement was received for this account; statements were requested",
    "page_not_read": "a page that couldn't be read was checked",
}


def _fmt(amount: str | None, currency: str | None) -> str | None:
    if amount is None:
        return None
    digits = 0 if currency == "KRW" else 2
    return f"{float(amount):,.{digits}f} {currency or ''}".strip()


def confined(path: Path, root: Path) -> Path:
    """The review folder must sit inside root (the working directory by default)."""
    root, target = root.resolve(), (root / path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"review folder {path} is outside {root}")
    return target


class ReviewError(Exception):
    pass


class Review:
    def __init__(self, inputs: Inputs, review_dir: Path, root: Path | None = None):
        self.inputs = inputs
        self.dir = confined(review_dir, root or Path.cwd())
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.audit_path = self.dir / "audit.jsonl"
        self.lock = threading.Lock()
        self.state = (
            json.loads(self.state_path.read_text())
            if self.state_path.exists()
            else {"acks": {}, "overrides": {}, "signoff": None, "summary": None}
        )
        self.worksheet = build(inputs, self.state["overrides"])
        self._revalidate_signoff()

    def _revalidate_signoff(self) -> None:
        """A sign-off covers the figures it fingerprinted. If the readings or intake changed since,
        the old sign-off no longer applies: keep it on record, clear it, and require a new review."""
        signed = self.state["signoff"]
        if signed and signed["hash"] != worksheet_hash(self.worksheet):
            self.state.setdefault("invalidated_signoffs", []).append(signed)
            self.state["signoff"] = None
            self.state["summary"] = None
            self._audit(
                "signoff_invalidated",
                "system",
                reason="the worksheet changed after sign-off",
                signed_hash=signed["hash"],
                current_hash=worksheet_hash(self.worksheet),
            )

    # -- views ---------------------------------------------------------------------------------
    def open_review_flags(self) -> list[str]:
        return [
            f["id"]
            for f in self.worksheet["flags"]
            if f["severity"] == "review" and f["id"] not in self.state["acks"]
        ]

    def view(self) -> dict:
        return {
            "worksheet": self.worksheet,
            "review": self.state,
            "open_review_flags": self.open_review_flags(),
            "audit": self.audit_tail(40),
        }

    def audit_tail(self, n: int) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [json.loads(x) for x in self.audit_path.read_text().splitlines()[-n:]]

    # -- actions -------------------------------------------------------------------------------
    def _audit(self, action: str, reviewer: str, **detail) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": action, "reviewer": reviewer, **detail}
        with self.audit_path.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))

    def _require_open(self) -> None:
        if self.state["signoff"]:
            raise ReviewError("The worksheet is signed off; it can't be changed.")

    def acknowledge(self, flag_id: str, note: str, reviewer: str) -> None:
        with self.lock:
            self._require_open()
            if flag_id not in {f["id"] for f in self.worksheet["flags"]}:
                raise ReviewError("That review item isn't on this worksheet.")
            if not note.strip():
                raise ReviewError("Add a note saying what you checked.")
            self.state["acks"][flag_id] = {"note": note.strip(), "reviewer": reviewer, "ts": time.time()}
            self._audit("acknowledge", reviewer, flag=flag_id, note=note.strip())

    def reopen(self, flag_id: str, reviewer: str) -> None:
        with self.lock:
            self._require_open()
            self.state["acks"].pop(flag_id, None)
            self._audit("reopen", reviewer, flag=flag_id)

    def set_ownership(self, account: str, ownership: str, note: str, reviewer: str) -> None:
        with self.lock:
            self._require_open()
            if ownership not in OWNERSHIPS:
                raise ReviewError(f"Ownership must be one of {', '.join(OWNERSHIPS)}.")
            if not note.strip():
                raise ReviewError("Add a note saying where the correction comes from.")
            before = next((a["ownership"] for a in self.worksheet["accounts"] if a["key"] == account), None)
            if before is None:
                raise ReviewError("That account isn't on this worksheet.")
            self.state["overrides"][account] = ownership
            self.worksheet = build(self.inputs, self.state["overrides"])
            self._audit(
                "correct_ownership",
                reviewer,
                account=account,
                before=before,
                after=ownership,
                note=note.strip(),
            )

    def sign_off(self, reviewer: str, initials: str) -> None:
        with self.lock:
            self._require_open()
            if not reviewer.strip() or not initials.strip():
                raise ReviewError("Enter your name and initials to sign off.")
            open_flags = self.open_review_flags()
            if open_flags:
                raise ReviewError(f"{len(open_flags)} review item(s) still need acknowledgement.")
            self.state["signoff"] = {
                "reviewer": reviewer.strip(),
                "initials": initials.strip(),
                "ts": time.strftime("%Y-%m-%d %H:%M"),
                "hash": worksheet_hash(self.worksheet),
            }
            self._audit("sign_off", reviewer, hash=self.state["signoff"]["hash"])

    def export(self) -> dict:
        if not self.state["signoff"]:
            raise ReviewError("Sign off before exporting.")
        if self.state["signoff"]["hash"] != worksheet_hash(self.worksheet):
            raise ReviewError("The worksheet changed after sign-off; review and sign off again.")
        return {
            "worksheet": self.worksheet,
            "signoff": self.state["signoff"],
            "acknowledgements": self.state["acks"],
            "corrections": self.state["overrides"],
            "audit": self.audit_tail(10_000),
        }

    # -- client summary ------------------------------------------------------------------------
    def summary_facts(self) -> dict:
        """Only the signed figures, formatted, with plain descriptions of what was reviewed.

        The CPA's acknowledgement notes are internal working-paper notes and are not passed on;
        account numbers are reduced to their last four digits; statement contents are not included.
        """
        ws = self.worksheet
        facts = []
        for a in ws["accounts"]:
            if not a["identified"]:
                continue
            reviews = [f for f in ws["flags"] if f["id"] in a["flags"] and f["severity"] == "review"]
            digits = re.sub(r"\D", "", a["account_number"])
            amount = a["max"]["amount"] if a["max"] else None
            facts.append(
                {
                    "bank": a["institutions"][-1] if a["institutions"] else None,
                    "account_ending": digits[-4:],
                    "currency": a["currency"],
                    "highest_balance": _fmt(amount, a["currency"]),
                    "highest_balance_usd": f"${a['max_usd']:,}" if a["max_usd"] is not None else None,
                    "what_your_cpa_reviewed": [CLIENT_LABELS.get(f["code"], f["code"]) for f in reviews],
                }
            )
        return {"client": ws["client"], "tax_year": ws["tax_year"], "accounts": facts}

    def draft_summary(self, generate) -> dict:
        """generate(facts, fresh) -> (text, meta). Runs only after sign-off; blocked if it leaves the scope.
        A redraft asks for a fresh generation, so a held draft can't be replayed forever."""
        if not self.state["signoff"]:
            raise ReviewError("Sign off before drafting the client summary.")
        text, meta = generate(self.summary_facts(), fresh=self.state["summary"] is not None)
        hit = OUT_OF_SCOPE.search(text)
        self.state["summary"] = {
            "text": text,
            "meta": meta,
            "approved": None,
            "blocked": f"contains '{hit.group(0)}'" if hit else None,
        }
        self._audit(
            "draft_summary",
            self.state["signoff"]["reviewer"],
            model=meta.get("model"),
            blocked=self.state["summary"]["blocked"],
        )
        return self.state["summary"]

    def approve_summary(self, reviewer: str) -> None:
        with self.lock:
            s = self.state["summary"]
            if not s:
                raise ReviewError("Draft the summary first.")
            if s["blocked"]:
                raise ReviewError(f"This draft can't be released: it {s['blocked']}. Draft it again.")
            s["approved"] = {"reviewer": reviewer, "ts": time.strftime("%Y-%m-%d %H:%M")}
            self._audit("approve_summary", reviewer)
