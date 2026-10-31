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
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from babel.numbers import format_currency

from ..reconcile.flags import CATALOG
from . import phrases as phrase_tables
from .phrases import LANGUAGES
from .worksheet import Inputs, build, worksheet_hash

OWNERSHIPS = ("sole", "joint", "signature_authority")
# The client summary is assembled from a fixed table of phrases, translated, plus the signed figures; the
# model writes no sentence of its own, so it can't add filing advice. OUT_OF_SCOPE is a second check on
# each translated phrase. Korean is matched by stem so polite inflections (신고하셔야 합니다) can't slip.
OUT_OF_SCOPE = re.compile(
    r"신고|제출\s*(?:하|해|의무|대상|필요)|의무|대상자|벌금|과태료|가산세|처벌|면제|납부|"
    r"\b(?:must|required to|need to|obligat\w*)\s+(?:file|report|submit)|\bpenalt|\bfines?\b|\bexempt",
    re.I,
)


# The summary's own words. Every line of the note is built from these and CLIENT_LABELS.
SUMMARY_PHRASES = {
    "heading": "What your CPA reviewed",
    "account_ending": "account ending",
    "highest_balance": "highest observed balance",
    "reviewed": "Items reviewed",
    "nothing_further": "none flagged",
    "balance_not_confirmed": "highest balance could not be established from the documents read",
    "bank_not_named": "bank name not available",
    "excluded_items": "Items left out of the totals",
}

# Client-facing descriptions of review items; the model translates these, never the CPA's notes.
CLIENT_LABELS = {
    code: definition.client_phrase
    for code, definition in CATALOG.items()
    if definition.severity == "review" and not definition.preparer_only
}


def _translation_problem(phrases: dict, got) -> str | None:
    """Why a translation can't be used, or None. Phrases carry no numbers, so a translation has none."""
    if not isinstance(got, dict) or set(got) != set(phrases):
        return "doesn't cover exactly the phrases asked for"
    for key, text in got.items():
        if (
            not isinstance(text, str)
            or not text.strip()
            or "\n" in text
            or ";" in text
            or re.search(r"\d", text)
        ):
            return f"has an unusable translation for '{key}'"
        if len(text) > 3 * len(phrases[key]) + 20:
            return f"has a translation for '{key}' far longer than the phrase"
        hit = OUT_OF_SCOPE.search(text)
        if hit:
            return f"contains '{hit.group(0)}'"
    return None


def _assemble(facts: dict, t: dict) -> str:
    lines = [t["heading"]]
    for a in facts["accounts"]:
        head = f"{a['bank'] or t['bank_not_named']} · {t['account_ending']} {a['account_ending']}"
        if a["highest_balance"]:
            usd = f" ({a['highest_balance_usd']})" if a["highest_balance_usd"] else ""
            head += f" · {t['highest_balance']} {a['highest_balance']}{usd}"
        else:
            head += f" · {t['balance_not_confirmed']}"
        items = [t[c] for c in a["review_items"]] or [t["nothing_further"]]
        lines += [head, f"  {t['reviewed']}: " + "; ".join(items)]
    if facts["excluded_items"]:
        lines.append(t["excluded_items"] + ": " + "; ".join(t[c] for c in facts["excluded_items"]))
    return "\n".join(lines)


def _money(amount: str | int | None, currency: str | None, language: str = "en_US") -> str | None:
    if amount is None:
        return None
    if not currency:
        return None
    return format_currency(Decimal(str(amount)), currency, format="#,##0.00 ¤¤", locale=language)


def confined(path: Path, root: Path) -> Path:
    """The review folder must sit inside root (the working directory by default)."""
    root, target = root.resolve(), (root / path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"review folder {path} is outside {root}")
    return target


class ReviewError(Exception):
    pass


def _account_context(value, key, path="worksheet"):
    if isinstance(value, dict):
        if value.get("key") == key or value.get("account") == key:
            return [(path, value)]
        matches = []
        for name, child in value.items():
            child_path = f"{path}/{name}"
            if name == key:
                matches.append((child_path, child))
            else:
                matches.extend(_account_context(child, key, child_path))
        return matches
    if isinstance(value, list):
        matches = []
        for child in value:
            matches.extend(_account_context(child, key, path))
        return matches
    return []


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
        self.state.setdefault("rates", {})
        self.state.setdefault("currencies", {})
        self.state.setdefault("interest", {})
        self.state.setdefault("ownership_details", {})
        self.worksheet = self._build()
        self._revalidate_signoff()

    def _build(self) -> dict:
        return build(
            self.inputs,
            self.state["overrides"],
            rate_overrides=self.state["rates"],
            currency_overrides=self.state["currencies"],
            interest_overrides=self.state["interest"],
            ownership_details=self.state["ownership_details"],
        )

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
    def _account_hash(self, key: str) -> str:
        # Discover complete account-keyed objects instead of maintaining per-field allowlists.
        # Lists keep their structural path rather than an index, so another account's insertion
        # doesn't invalidate this account's review.
        matches = _account_context(self.worksheet, key)
        panel = self.worksheet["form8938"]
        # Exclude only account containers and aggregate results; new shared fields count by default.
        shared = {
            k: v for k, v in self.worksheet.items() if k not in {"accounts", "flags", "totals", "form8938"}
        }
        shared_panel = {
            k: v
            for k, v in panel.items()
            if k
            not in {
                "accounts",
                "stated_accounts",
                "effective_accounts",
                "corrections",
                "year_end",
                "any_time",
                "notes",
            }
        }
        return worksheet_hash({"shared": shared, "panel": shared_panel, "account_context": matches})

    def _ack_valid(self, flag: dict) -> bool:
        ack = self.state["acks"].get(flag["id"])
        return bool(ack and ack.get("account_hash") == self._account_hash(flag["account"]))

    def _valid_acknowledgements(self) -> dict:
        return {
            flag["id"]: self.state["acks"][flag["id"]]
            for flag in self.worksheet["flags"]
            if self._ack_valid(flag)
        }

    def open_review_flags(self) -> list[str]:
        return [
            f["id"] for f in self.worksheet["flags"] if f["severity"] == "review" and not self._ack_valid(f)
        ]

    def remaining_review_items(self) -> list[dict]:
        """Acknowledgement records review; it does not prove a still-present flag was resolved."""
        acknowledgements = self._valid_acknowledgements()
        return [
            {**flag, "acknowledgement": acknowledgements.get(flag["id"])}
            for flag in self.worksheet["flags"]
            if flag["severity"] == "review"
        ]

    def open_determinations(self) -> list[dict]:
        """Surface unfinished comparisons and incomplete evidence alongside signed review items."""
        out = []
        totals = self.worksheet["totals"]
        if totals["above_threshold"] is None:
            out.append(
                {
                    "kind": "fbar_threshold",
                    "message": "Below-threshold result is undetermined; exclusions or review may change it.",
                    "reasons": totals["threshold_review_reasons"],
                }
            )
        elif totals["crossing_depends_on_flags"]:
            out.append(
                {"kind": "fbar_threshold", "message": "FBAR threshold comparison depends on review items."}
            )
        if totals["accounts_without_value"]:
            out.append(
                {
                    "kind": "fbar_missing_values",
                    "message": "Accounts without a determinable value are excluded from the aggregate.",
                    "accounts": totals["accounts_without_value"],
                }
            )
        if totals["unidentified_uploads"]:
            out.append(
                {
                    "kind": "fbar_unidentified",
                    "message": "Unidentified rows or pages are excluded from the aggregate.",
                    "uploads": totals["unidentified_uploads"],
                }
            )
        panel = self.worksheet["form8938"]
        for period, bound in (("year_end", "year_end"), ("any_time", "maximum")):
            comparison = panel[period]
            if comparison["result"] in ("above", "not_above") and comparison["upper_known"]:
                continue
            accounts = [
                a["number"] or ", ".join(a.get("uploads", [])) or a["key"]
                for a in panel["accounts"]
                if a["inclusion"] != "excluded"
                and (
                    a["inclusion"] == "unresolved"
                    or a.get("share") == "uncertain_share"
                    or a.get(bound, {}).get("high_usd") is None
                    or bool(a.get("reviews"))
                )
            ]
            out.append(
                {
                    "kind": "form8938_" + period,
                    "label": "Form 8938 · " + panel["labels"][period],
                    "result": comparison["result"],
                    "message": comparison["text"]
                    + (" Upper bound is unknown." if not comparison["upper_known"] else ""),
                    "accounts": accounts,
                    "scope": panel["labels"]["scope"],
                }
            )
        return out

    def view(self) -> dict:
        return {
            "worksheet": self.worksheet,
            "review": {**self.state, "acks": self._valid_acknowledgements()},
            "open_review_flags": self.open_review_flags(),
            "remaining_review_items": self.remaining_review_items() if self.state["signoff"] else [],
            "open_determinations": self.open_determinations() if self.state["signoff"] else [],
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

    def acknowledge(self, flag_id: str, note: str, reviewer: str, *, rate: dict | None = None) -> None:
        with self.lock:
            self._require_open()
            flag = next((f for f in self.worksheet["flags"] if f["id"] == flag_id), None)
            if flag is None:
                raise ReviewError("That review item isn't on this worksheet.")
            if not note.strip():
                raise ReviewError("Add a note saying what you checked.")
            account = next(a for a in self.worksheet["accounts"] if a["key"] == flag["account"])
            if flag["code"] == "currency_conflict" and not account["currency_correction"]:
                raise ReviewError("Select the account currency with a source note before acknowledgement.")
            if rate is not None:
                if flag["code"] != "currency_unknown":
                    raise ReviewError("An alternative rate belongs to a missing Treasury rate review item.")
                saved = self._validate_rate(rate, account)
                self.state["rates"][account["key"]] = saved
                self.worksheet = self._build()
                self._audit(
                    "record_exchange_rate", reviewer, account=account["key"], rate=saved, note=note.strip()
                )
            self.state["acks"][flag_id] = {
                "note": note.strip(),
                "reviewer": reviewer,
                "ts": time.time(),
                "account_hash": self._account_hash(flag["account"]),
            }
            self._audit("acknowledge", reviewer, flag=flag_id, note=note.strip())

    def reopen(self, flag_id: str, reviewer: str) -> None:
        with self.lock:
            self._require_open()
            flag = next((f for f in self.worksheet["flags"] if f["id"] == flag_id), None)
            if flag and flag["code"] == "currency_unknown":
                self.state["rates"].pop(flag["account"], None)
                self.worksheet = self._build()
            self.state["acks"].pop(flag_id, None)
            self._audit("reopen", reviewer, flag=flag_id)

    def _validate_rate(self, rate: dict, account: dict) -> dict:
        try:
            value = Decimal(str(rate["units_per_usd"]))
            record_date = date.fromisoformat(rate["record_date"])
            source = rate["source"]
            if not isinstance(source, str):
                raise ValueError("source must be text")
            source = source.strip()
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ReviewError("Enter a numeric rate, source and ISO date.") from None
        if not value.is_finite() or value <= 0 or not source:
            raise ReviewError("The rate must be positive and finite, with a source.")
        if not account["currency"]:
            raise ReviewError("Resolve the account currency before entering an exchange rate.")
        if record_date != date(self.inputs.tax_year, 12, 31):
            raise ReviewError("Use a verifiable rate for the last day of the tax year.")
        return {
            "currency": account["currency"],
            "units_per_usd": str(value),
            "source": source,
            "record_date": record_date.isoformat(),
        }

    def set_currency(self, account: str, currency: str, note: str, reviewer: str) -> None:
        with self.lock:
            self._require_open()
            row = next((a for a in self.worksheet["accounts"] if a["key"] == account), None)
            if row is None or not row["identified"]:
                raise ReviewError("Select an identified account on this worksheet.")
            if currency not in row["observed_currencies"] or not re.fullmatch(r"[A-Z]{3}", currency):
                raise ReviewError("Select one of the currencies printed on the account statements.")
            if not note.strip():
                raise ReviewError("Record a source note explaining the currency correction.")
            before = row["currency"]
            self.state["currencies"][account] = {
                "currency": currency,
                "note": note.strip(),
                "reviewer": reviewer,
                "ts": time.time(),
            }
            self.state["rates"].pop(account, None)
            # Every acknowledgement for this account covered the previous interpretation.
            for flag in self.worksheet["flags"]:
                if flag["account"] == account:
                    self.state["acks"].pop(flag["id"], None)
            self.worksheet = self._build()
            self._audit(
                "correct_currency",
                reviewer,
                account=account,
                before=before,
                after=currency,
                note=note.strip(),
            )

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
            self.state["ownership_details"][account] = {
                "value": ownership,
                "note": note.strip(),
                "reviewer": reviewer,
                "ts": time.time(),
            }
            self.worksheet = self._build()
            self._audit(
                "correct_ownership",
                reviewer,
                account=account,
                before=before,
                after=ownership,
                note=note.strip(),
            )

    def set_financial_interest(self, account: str, value: bool | None, note: str, reviewer: str) -> None:
        with self.lock:
            self._require_open()
            if value is not None and type(value) is not bool:
                raise ReviewError("Financial interest must be true, false or unknown.")
            if not note.strip():
                raise ReviewError("Record the source of the financial interest correction.")
            row = next((a for a in self.worksheet["accounts"] if a["key"] == account), None)
            if row is None or not row["identified"]:
                raise ReviewError("Select an identified account on this worksheet.")
            before = row["financial_interest"]
            self.state["interest"][account] = {
                "value": value,
                "note": note.strip(),
                "reviewer": reviewer,
                "ts": time.time(),
            }
            self.worksheet = self._build()
            self._audit(
                "correct_financial_interest",
                reviewer,
                account=account,
                before=before,
                after=value,
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
            "figure_labels": {
                "max": "highest observed balance",
                "max_usd": "highest observed balance in USD",
                "aggregate_usd": "aggregate of highest observed balances in USD",
            },
            "signoff": self.state["signoff"],
            "acknowledgements": self._valid_acknowledgements(),
            "remaining_review_items": self.remaining_review_items(),
            "open_determinations": self.open_determinations(),
            "corrections": self.state["overrides"],
            "audit": self.audit_tail(10_000),
        }

    # -- client summary ------------------------------------------------------------------------
    def summary_facts(self, language: str = "en_US") -> dict:
        """Only the signed figures, formatted, with plain descriptions of what was reviewed.

        The CPA's acknowledgement notes are internal working-paper notes and are not passed on;
        account numbers are reduced to their last four digits; statement contents are not included.
        """
        ws = self.worksheet
        facts, excluded = [], []
        for a in ws["accounts"]:
            if not a["identified"]:
                excluded.extend(
                    f["code"]
                    for f in ws["flags"]
                    if f["id"] in a["flags"]
                    and f["code"] in {"account_number_masked", "account_unidentified", "page_not_read"}
                )
                continue
            reviews = [
                f
                for f in ws["flags"]
                if f["id"] in a["flags"]
                and f["severity"] == "review"
                and not CATALOG[f["code"]].preparer_only
            ]
            digits = re.sub(r"\D", "", a["account_number"])
            amount = a["max"]["amount"] if a["max"] else None
            facts.append(
                {
                    "bank": a["institutions"][-1] if a["institutions"] else None,
                    "account_ending": digits[-4:],
                    "currency": a["currency"],
                    "highest_balance": _money(amount, a["currency"], language),
                    "highest_balance_usd": _money(a["max_usd"], "USD", language),
                    "review_items": list(dict.fromkeys(f["code"] for f in reviews)),
                }
            )
        return {
            "client": ws["client"],
            "tax_year": ws["tax_year"],
            "accounts": facts,
            "excluded_items": list(dict.fromkeys(excluded)),
        }

    def draft_summary(self, translate, language: str = "ko") -> dict:
        """translate(phrases, language, fresh) -> (translations, meta). Runs only after sign-off.

        The note is assembled from fixed phrases and the signed figures. Phrases come from the language's
        reviewed table; only phrases missing from it are sent to the model, and the draft lists them as
        unreviewed. A translation that adds keys, digits, line breaks or out-of-scope words holds the draft
        back. A redraft asks for a fresh translation, so a held draft can't be replayed."""
        if not self.state["signoff"]:
            raise ReviewError("Sign off before drafting the client summary.")
        if language not in LANGUAGES:
            raise ReviewError(f"No client language {language!r}.")
        facts = self.summary_facts(language)
        codes = {c for a in facts["accounts"] for c in a["review_items"]} | set(facts["excluded_items"])
        if codes - set(CLIENT_LABELS):
            raise ReviewError(f"No client wording for {sorted(codes - set(CLIENT_LABELS))}.")
        phrases = {**SUMMARY_PHRASES, **{c: CLIENT_LABELS[c] for c in sorted(codes)}}
        known = phrase_tables.reviewed(language, phrases)
        catalog_drafts = {k: v for k, v in phrase_tables.drafts(language, phrases).items() if k not in known}
        missing = {k: v for k, v in phrases.items() if k not in known and k not in catalog_drafts}
        meta, problem, new = {"model": None, "source": f"gettext catalog ({language})"}, None, {}
        if missing:
            new, meta = translate(missing, LANGUAGES[language], fresh=self.state["summary"] is not None)
            problem = _translation_problem(missing, new)
            if problem:
                new = {}
        unreviewed = {**catalog_drafts, **new}
        if not problem:
            problem = _translation_problem(phrases, {**known, **unreviewed})
        self.state["summary"] = {
            "text": None if problem else _assemble(facts, {**known, **unreviewed}),
            "language": language,
            "unreviewed": unreviewed if not problem else {},
            "meta": meta,
            "approved": None,
            "blocked": problem,
        }
        self._audit(
            "draft_summary",
            self.state["signoff"]["reviewer"],
            model=meta.get("model"),
            unreviewed=sorted(unreviewed),
            blocked=problem,
        )
        return self.state["summary"]

    def approve_summary(self, reviewer: str, confirm_unreviewed: bool = False) -> None:
        """Phrases translated for this draft, not taken from a reviewed table, are approved only when the
        CPA confirms them; they are listed in the draft and recorded with the approval."""
        with self.lock:
            s = self.state["summary"]
            if not s:
                raise ReviewError("Draft the summary first.")
            if s["blocked"]:
                raise ReviewError(f"This draft can't be released: it {s['blocked']}. Draft it again.")
            if s["unreviewed"] and not confirm_unreviewed:
                raise ReviewError(
                    f"{len(s['unreviewed'])} phrases were translated for this draft and haven't been "
                    "reviewed. Read them and confirm to approve."
                )
            s["approved"] = {"reviewer": reviewer, "ts": time.strftime("%Y-%m-%d %H:%M")}
            self._audit("approve_summary", reviewer, confirmed_unreviewed=sorted(s["unreviewed"]))
