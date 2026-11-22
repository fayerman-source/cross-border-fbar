"""Draft visual review workpaper PDF from a signed worksheet, for preparer verification.

Renders account figures, FinCEN Form 114 part classifications, cropped source-line image strips (when
grounding boxes are verified), remaining review flags, preparer acknowledgement notes, open threshold
determinations, and audit history into a standalone PDF. Nothing here determines whether anyone files.
"""

from __future__ import annotations

import io
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw

from ..synth.render import FONT_FILES, static_font
from .csv_export import DRAFT_NOTE, _fincen_part, _format_determination

FONTS_DIR = Path(__file__).resolve().parents[1] / "fonts"


def _crop_source_strip(set_dir: Path | None, source: dict | None) -> io.BytesIO | None:
    if set_dir is None or not source:
        return None
    bbox = source.get("bbox")
    upload = source.get("upload")
    if not bbox or not upload or not any(bbox.values()):
        return None
    pages_root = (set_dir / "pages").resolve()
    img_path = (set_dir / upload).resolve()
    if img_path.parent != pages_root or not img_path.is_file():
        return None
    with Image.open(img_path) as raw:
        rgb = raw.convert("RGB")
        w, h = rgb.size
        if w <= 0 or h <= 0:
            return None
        pad = 16
        y0 = max(0, int((bbox["y_min"] - pad) * h / 1000))
        y1 = min(h, max(y0 + 1, int((bbox["y_max"] + pad) * h / 1000)))
        strip = rgb.crop((0, y0, w, y1))
        draw = ImageDraw.Draw(strip)
        bx0 = max(0, int(bbox["x_min"] * w / 1000))
        bx1 = min(w - 1, int(bbox["x_max"] * w / 1000))
        by0 = max(0, int(bbox["y_min"] * h / 1000) - y0)
        by1 = min(strip.height - 1, int(bbox["y_max"] * h / 1000) - y0)
        if bx1 > bx0 and by1 > by0:
            draw.rectangle((bx0, by0, bx1, by1), outline=(196, 120, 0), width=2)
        buf = io.BytesIO()
        strip.save(buf, format="PNG")
        buf.seek(0)
        return buf


def _new_pdf() -> FPDF:
    pdf = FPDF(unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    for family in FONT_FILES:
        pdf.add_font(family, "", str(static_font(FONTS_DIR, family, 400)))
        pdf.add_font(family, "B", str(static_font(FONTS_DIR, family, 700)))
    pdf.set_fallback_fonts(["kr", "th"])
    pdf.set_text_shaping(True)
    return pdf


def _write_wrapped(
    pdf: FPDF, text: str, *, size: float = 8.5, bold: bool = False, grey: bool = False
) -> None:
    pdf.set_font("latin", "B" if bold else "", size)
    pdf.set_text_color(100 if grey else 20)
    pdf.multi_cell(0, 4.8, text, new_x="LMARGIN", new_y="NEXT")


def _format_correction_line(corr: dict) -> str:
    read_info = corr.get("read") or {}
    orig = read_info.get("text") or read_info.get("value") or "nothing"
    src_wp = (corr.get("source") or {}).get("wp") or corr.get("upload") or ""
    typed = corr.get("text") or corr.get("value") or ""
    return (
        f"Reviewer correction · {src_wp} ({corr['field']}): read {orig} -> "
        f"corrected to {corr['value']} ({typed}) by {corr['reviewer']} · Note: {corr['note']}"
    )


def _format_rate_summary(acct: dict) -> str:
    if not acct.get("rate"):
        return "n/a (n/a)"
    raw_basis = acct.get("rate_basis") or "n/a"
    basis = raw_basis if raw_basis in ("treasury", "identity", "n/a") else "preparer-recorded"
    meta = [basis]
    if acct.get("rate_date"):
        meta.append(f"as of {acct['rate_date']}")
    if acct.get("rate_source"):
        meta.append(f"source: {acct['rate_source']}")
    return f"{acct['rate']} ({', '.join(meta)})"


def _format_audit_entry(entry: dict) -> str:
    who = entry.get("reviewer") or entry.get("initials") or ""
    if entry.get("upload") and entry.get("field"):
        target = f"{entry['upload']} {entry['field']}"
    else:
        target = entry.get("flag") or entry.get("field") or entry.get("account") or entry.get("hash", "")[:12]
    change = f"{entry.get('before')} -> {entry.get('after')}" if "before" in entry else ""
    rate_obj = entry.get("rate")
    rate_detail = (
        f"{rate_obj.get('currency', '')} {rate_obj.get('units_per_usd', '')} "
        f"as of {rate_obj.get('record_date', '')} ({rate_obj.get('source', '')})".strip()
        if isinstance(rate_obj, dict)
        else ""
    )
    note = f"Note: {entry['note']}" if entry.get("note") else ""
    parts = [
        p for p in (entry.get("ts", ""), entry.get("action", ""), who, target, change, rate_detail, note) if p
    ]
    return " · ".join(parts)


def _render_account_block(
    pdf: FPDF,
    acct: dict,
    set_dir: Path | None,
    items: list[dict],
    corrections: list[dict],
) -> None:
    if pdf.get_y() > 245:
        pdf.add_page()
    institutions = "; ".join(acct["institutions"]) if acct["institutions"] else "Institution not stated"
    acct_label = acct["account_number"] or "; ".join(acct["uploads"])
    usd_text = f"${acct['max_usd']:,}" if acct["max_usd"] is not None else "UNDETERMINED"
    _write_wrapped(
        pdf,
        f"{institutions} · {acct_label} · {_fincen_part(acct)} · Max USD: {usd_text}",
        size=9.5,
        bold=True,
    )

    holders = "; ".join(acct["holders"]) if acct["holders"] else "Not stated"
    max_info = acct["max"]
    if max_info:
        kind_label = (
            f"{max_info['kind']}, reviewer-corrected" if max_info.get("correction") else max_info["kind"]
        )
        native_line = (
            f"Holders: {holders} · Max observed: {max_info['amount']} {acct['currency'] or ''} "
            f"({kind_label}, {max_info['day'] or 'date unstated'}) · "
            f"Rate: {_format_rate_summary(acct)} · "
            f"Source: {max_info['source']['wp']}"
        )
    else:
        native_line = (
            f"Holders: {holders} · Max observed: UNDETERMINED · "
            f"Uploads: {'; '.join(acct['uploads']) or 'none'}"
        )
    _write_wrapped(pdf, native_line, size=8.2)

    effective_corrections = corrections
    if not effective_corrections and max_info and max_info.get("correction"):
        effective_corrections = [max_info["correction"]]
    for corr in effective_corrections:
        _write_wrapped(pdf, _format_correction_line(corr), size=8, grey=True)

    strip = _crop_source_strip(set_dir, max_info["source"] if max_info else None)
    if strip is not None:
        if pdf.get_y() > 250:
            pdf.add_page()
        pdf.image(strip, x=pdf.l_margin, w=180)
        pdf.ln(1)

    for item in items:
        ack = item.get("acknowledgement")
        ack_suffix = f" [Ack by {ack['reviewer']}: {ack['note']}]" if ack else ""
        _write_wrapped(pdf, f"Review flag · {item['code']}: {item['message']}{ack_suffix}", size=8, grey=True)
    pdf.ln(2)


def export_workpaper_pdf(exported: dict, set_dir: Path | None = None) -> bytes:
    """Render a signed worksheet export dict as a draft PDF workpaper for preparer check."""
    ws = exported["worksheet"]
    signoff = exported["signoff"]
    totals = ws["totals"]
    above = totals["above_threshold"]
    threshold_state = "undetermined" if above is None else ("above" if above else "not_above")

    remaining_by_account: dict[str, list[dict]] = {}
    for item in exported["remaining_review_items"]:
        remaining_by_account.setdefault(item["account"], []).append(item)

    corrections_by_account: dict[str, list[dict]] = {}
    for corr in ws.get("value_corrections") or []:
        corrections_by_account.setdefault(corr["account"], []).append(corr)

    pdf = _new_pdf()
    pdf.add_page()

    _write_wrapped(
        pdf,
        f"FBAR Review Workpaper Draft (FinCEN Form 114) · Tax Year {ws['tax_year']}",
        size=13,
        bold=True,
    )
    _write_wrapped(
        pdf,
        f"{DRAFT_NOTE}. Balances, thresholds and review flags only.",
        size=8.5,
        grey=True,
    )
    pdf.ln(1.5)
    _write_wrapped(
        pdf,
        f"Client: {ws['client'] or 'Not stated'} · Signed by: {signoff['reviewer']} ({signoff['initials']}) "
        f"at {signoff['ts']}",
        size=9,
    )
    _write_wrapped(pdf, f"Worksheet SHA-256: {signoff['hash']}", size=8, grey=True)
    _write_wrapped(
        pdf,
        f"Aggregate highest observed balance: ${totals['aggregate_usd']:,} · "
        f"Threshold: ${totals['threshold_usd']:,} · Comparison: {threshold_state}",
        size=9.5,
        bold=True,
    )
    pdf.ln(2.5)

    _write_wrapped(pdf, "Schedule A · Accounts and Source Evidence", size=11, bold=True)
    pdf.ln(1)

    for acct in ws["accounts"]:
        _render_account_block(
            pdf,
            acct,
            set_dir,
            remaining_by_account.get(acct["key"], []),
            corrections_by_account.get(acct["key"], []),
        )

    if exported["open_determinations"]:
        if pdf.get_y() > 245:
            pdf.add_page()
        _write_wrapped(pdf, "Schedule B · Open Determinations", size=11, bold=True)
        for det in exported["open_determinations"]:
            _write_wrapped(pdf, f"· {_format_determination(det)}", size=8.5)
        pdf.ln(2)

    audit_entries = exported.get("audit") or []
    if audit_entries:
        if pdf.get_y() > 245:
            pdf.add_page()
        _write_wrapped(pdf, "Schedule C · Review Audit Log (Recent Entries)", size=11, bold=True)
        for entry in audit_entries[-40:]:
            _write_wrapped(pdf, _format_audit_entry(entry), size=7.8, grey=True)

    return bytes(pdf.output())
