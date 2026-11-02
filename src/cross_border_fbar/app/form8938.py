"""Read-only, indicative preparer view. All comparison arithmetic lives in reconciliation."""

from dataclasses import asdict
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from ..reconcile.form8938 import form8938_totals
from ..reconcile.observe import Source

LABELS = {
    "title": "Form 8938 · indicative preparer review",
    "scope": "Accounts read only; not an inventory of specified foreign financial assets. "
    "Other assets, spouse-only accounts, client eligibility and return exceptions are not assessed. "
    "This comparison does not determine whether anyone files.",
    "intake": "Stated at intake; the preparer determines filing status and qualifying foreign residence. "
    "Living overseas alone does not establish qualifying foreign residence.",
    "annual": "The any-time range bounds the accounts read; it is not an exact daily aggregate. "
    "Individual account maxima can occur on different days.",
    "precision": "USD bounds are rounded outward for display; extra digits appear near a threshold. "
    "Exact bounds remain in the export. A finite upper bound does not establish a complete asset inventory.",
    "year_end": "Last day of the tax year",
    "any_time": "Any time during the tax year",
    "lower": "Lower bound (USD)",
    "upper": "Upper bound (USD)",
    "threshold": "Threshold (strictly more than)",
    "unknown": "Unknown",
    "missing_intake": "Not stated",
    "review": "Review",
    "evidence": "Candidate evidence and source lines",
    "year_end_sources": "Year-end evidence",
    "maximum_sources": "Annual maximum evidence",
    "included": "Included: financial interest stated",
    "excluded": "Excluded: no financial interest stated",
    "unresolved": "Unresolved inclusion",
    "full": "Full value",
    "half": "Half value",
    "uncertain_share": "Share unresolved: half-to-full bounds",
    "classification_review": "Review classification: signature authority and financial interest "
    "are both stated. "
    "Financial interest remains counted; neither fact has been changed.",
    "year_end_missing": "Review: year-end upper bound is unknown",
    "maximum_missing": "Review: annual maximum upper bound is unknown",
    "year_end_formula": "Year-end: sum of the included accounts' year-end bounds.",
    "any_time_formula": "Any-time lower: the greater of the year-end lower total and largest individual "
    "maximum lower bound. Upper: sum of individual maximum upper bounds; unknown if any are unresolved.",
}
RESULT_TEXT = {
    "above": "Known lower bound exceeds the threshold. Review",
    "not_above": "Upper bound for accounts read does not exceed the threshold. Review",
    "between": "Threshold falls inside the range. Review",
    "incomplete": "Upper bound is unknown; comparison unresolved. Review",
    "needs_intake": "Threshold awaits filing status and qualifying residence. Review",
}
HOUSEHOLD_LABELS = {
    "filing_status": "Filing status",
    "lives_abroad": "Qualifying foreign residence",
    "spouse_specified_individual": "Spouse is a specified individual",
}


def _decimal(value):
    return format(value, "f") if value is not None else None


def _display_bound(value, *, upper, threshold=None):
    if value is None:
        return None
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits) + abs(value.as_tuple().exponent) + 4)
        rounding = ROUND_CEILING if upper else ROUND_FLOOR
        shown = value.quantize(Decimal("0.01"), rounding=rounding)
    if threshold is not None and (value > threshold) != (shown > threshold):
        return _decimal(value)
    return _decimal(shown)


def _range(low, high, threshold=None):
    return {
        "low_usd": _decimal(low),
        "high_usd": _decimal(high),
        "display_low_usd": _display_bound(low, upper=False, threshold=threshold),
        "display_high_usd": _display_bound(high, upper=True, threshold=threshold),
    }


def _test_view(test):
    return {
        **_range(test.low_usd, test.high_usd, test.threshold_usd),
        "threshold_usd": str(test.threshold_usd) if test.threshold_usd is not None else None,
        "upper_known": test.complete,
        "result": test.result,
        "text": RESULT_TEXT[test.result],
    }


def _account_view(account, contribution, totals, source_view):
    key = account.account_key
    row = {
        "key": key,
        "number": account.account_number,
        "identified": account.identified,
        "inclusion": "included" if contribution else "excluded",
        "reason": totals.excluded.get(key),
        "uploads": account.uploads,
        "upload_sources": [source_view(Source(upload, "uploaded page", None)) for upload in account.uploads],
        "ownership": account.ownership,
        "financial_interest": account.financial_interest,
        "year_end_sources": [source_view(s) for s in account.year_end_sources],
        "maximum_sources": [source_view(s) for s in account.maximum_sources],
        "reviews": [],
    }
    if key in totals.unresolved_accounts:
        row.update(inclusion="unresolved", reason=totals.unresolved_accounts[key])
    if account.ownership == "signature_authority" and account.financial_interest is True:
        row["reviews"].append(LABELS["classification_review"])
    if contribution:
        row["year_end"] = _range(contribution.year_end_low, contribution.year_end_high)
        row["maximum"] = _range(contribution.maximum_low, contribution.maximum_high)
        row["share"] = "half" if contribution.half else "full"
        if contribution.note:
            row.update(share="uncertain_share", share_note=contribution.note)
        if contribution.year_end_high is None:
            row["reviews"].append(LABELS["year_end_missing"])
        if contribution.maximum_high is None:
            row["reviews"].append(LABELS["maximum_missing"])
    return row


def build_panel(results, document, intake, source_view):
    totals = form8938_totals(results, document.household, intake)
    contributions = {item.key: item for item in totals.contributions}
    return {
        "labels": LABELS,
        "household_labels": HOUSEHOLD_LABELS,
        "household": asdict(document.household),
        "intake_source": document.source,
        "stated_accounts": {key: asdict(value) for key, value in document.accounts.items()},
        "effective_accounts": {key: asdict(value) for key, value in intake.items()},
        "intake_metadata": document.metadata,
        "year_end": _test_view(totals.year_end),
        "any_time": _test_view(totals.any_time),
        "accounts": [
            _account_view(a, contributions.get(a.account_key), totals, source_view) for a in results
        ],
        "notes": totals.notes,
    }
