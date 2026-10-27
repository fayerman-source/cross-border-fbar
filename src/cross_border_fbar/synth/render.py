"""Render statements to PDF in four bank layouts, recording where every field is drawn.

Positions are returned as boxes on a 0-1000 grid of the page (x0, y0, x1, y1 from
the top-left), the same convention the model's grounding output uses, so the
ground truth can score boxes directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fpdf import FPDF

from .model import EXPONENT, AccountSpec, Statement, Txn

PAGE_W, PAGE_H = 210.0, 297.0  # A4, mm
MARGIN = 15.0
ROW_H = 6.5
BOTTOM = PAGE_H - 22.0
FIXED_DATE = datetime(2026, 1, 15, tzinfo=timezone.utc)  # keeps PDFs byte-identical across runs
FOOTER = "SYNTHETIC TEST DOCUMENT - fictional bank and account holder - not a real statement"


@dataclass(frozen=True)
class Column:
    key: str  # no | date | value | desc | amount | debit | credit | balance
    label: str
    width: float
    align: str


@dataclass(frozen=True)
class Layout:
    font: str
    title: str
    columns: tuple[Column, ...]
    prints_running_balance: bool


LAYOUTS = {
    "kr": Layout(
        "kr",
        "거래내역서",
        (
            Column("no", "번호", 10, "C"),
            Column("date", "거래일자", 24, "C"),
            Column("desc", "적요", 62, "L"),
            Column("debit", "출금액", 28, "R"),
            Column("credit", "입금액", 28, "R"),
            Column("balance", "잔액", 28, "R"),
        ),
        True,
    ),
    "de_plain": Layout(
        "latin",
        "Kontoauszug",
        (
            Column("date", "Buchungstag", 24, "L"),
            Column("value", "Wert", 22, "L"),
            Column("desc", "Buchungstext", 96, "L"),
            Column("amount", "Betrag EUR", 38, "R"),
        ),
        False,
    ),
    "de_saldo": Layout(
        "latin",
        "Kontoauszug",
        (
            Column("date", "Datum", 20, "L"),
            Column("value", "Valuta", 20, "L"),
            Column("desc", "Verwendungszweck", 68, "L"),
            Column("debit", "Soll", 24, "R"),
            Column("credit", "Haben", 24, "R"),
            Column("balance", "Saldo", 24, "R"),
        ),
        True,
    ),
    "th": Layout(
        "th",
        "รายการเคลื่อนไหวบัญชี / STATEMENT OF ACCOUNT",
        (
            Column("date", "วันที่", 24, "C"),
            Column("desc", "รายการ", 70, "L"),
            Column("debit", "ถอน", 28, "R"),
            Column("credit", "ฝาก", 28, "R"),
            Column("balance", "คงเหลือ", 30, "R"),
        ),
        True,
    ),
}


def box(x: float, y: float, w: float, h: float) -> dict[str, int]:
    return {
        "x0": round(x / PAGE_W * 1000),
        "y0": round(y / PAGE_H * 1000),
        "x1": round((x + w) / PAGE_W * 1000),
        "y1": round((y + h) / PAGE_H * 1000),
    }


def fmt_amount(minor: int, currency: str, style: str) -> str:
    exp = EXPONENT[currency]
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 10**exp) if exp else (abs(minor), 0)
    if style == "de":
        grouped = f"{whole:,}".replace(",", ".")
        return f"{sign}{grouped},{frac:02d}"
    grouped = f"{whole:,}"
    return f"{sign}{grouped}.{frac:0{exp}d}" if exp else f"{sign}{grouped}"


def fmt_date(d: date, layout: str) -> str:
    if layout == "kr":
        return d.strftime("%Y.%m.%d")
    if layout == "th":
        return f"{d.day:02d}/{d.month:02d}/{d.year + 543}"  # Buddhist era
    return d.strftime("%d.%m.%Y")


def _month_end(year: int, month: int) -> date:
    return date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


FONT_FILES = {"latin": "NotoSans-VF.ttf", "kr": "NotoSansKR-VF.ttf", "th": "NotoSansThai-VF.ttf"}
FONT_CACHE = Path(__file__).resolve().parents[3] / ".cache" / "fonts"


def static_font(fonts: Path, family: str, weight: int) -> Path:
    """A static instance of a variable font, built once and cached.

    Instancing the Korean variable font takes several seconds, so doing it per
    statement made a full run take many minutes.
    """
    out = FONT_CACHE / f"{family}-{weight}.ttf"
    if not out.exists():
        from fontTools.ttLib import TTFont
        from fontTools.varLib import instancer

        out.parent.mkdir(parents=True, exist_ok=True)
        instancer.instantiateVariableFont(TTFont(fonts / FONT_FILES[family]), {"wght": weight}).save(out)
    return out


class _Doc:
    """One statement PDF plus the positions of what was drawn on it."""

    def __init__(self, fonts: Path, layout_key: str):
        self.layout_key = layout_key
        self.layout = LAYOUTS[layout_key]
        pdf = FPDF(unit="mm", format="A4")
        pdf.set_auto_page_break(False)
        pdf.set_creation_date(FIXED_DATE)
        pdf.set_producer("cross-border-fbar synthetic generator")
        for family in FONT_FILES:
            pdf.add_font(family, "", str(static_font(fonts, family, 400)))
            pdf.add_font(family, "B", str(static_font(fonts, family, 700)))
        pdf.set_fallback_fonts(["latin"])
        pdf.set_text_shaping(True)
        self.pdf = pdf
        self.page = 0

    def text(self, x, y, w, h, s, size=9, bold=False, align="L", font=None, grey=False):
        pdf = self.pdf
        pdf.set_font(font or self.layout.font, "B" if bold else "", size)
        pdf.set_text_color(120 if grey else 20)
        while pdf.get_string_width(s) > w - 1 and len(s) > 4:
            s = s[:-2] + "…"
        pdf.set_xy(x, y)
        pdf.cell(w, h, s, align=align)
        # The ink, not the cell: models box printed text tightly, so scoring
        # their boxes against whole cells understates them (step-1 probe, Oct 27).
        sw, sh = pdf.get_string_width(s), size * 0.3528  # pt -> mm
        tx = {"L": x + pdf.c_margin, "R": x + w - pdf.c_margin - sw, "C": x + (w - sw) / 2}[align]
        return {"page": self.page, "bbox": box(x, y, w, h), "text_bbox": box(tx, y + (h - sh) / 2, sw, sh)}

    def new_page(self):
        self.pdf.add_page()
        self.page += 1


def render_statement(spec: AccountSpec, stmt: Statement, fonts: Path, out: Path) -> dict:
    """Write one statement PDF; return its fields and line positions for the ground truth."""
    doc = _Doc(fonts, stmt.bank.layout)
    lay, key, cur = doc.layout, doc.layout_key, spec.currency
    style = "de" if key.startswith("de") else "plain"
    money = lambda m: fmt_amount(m, cur, style)  # noqa: E731
    start, end = date(stmt.year, stmt.month, 1), _month_end(stmt.year, stmt.month)
    fields: dict[str, dict] = {}

    def field(name, value, pos):
        fields[name] = {"value": value, **pos}

    def header(first: bool) -> float:
        doc.new_page()
        x, y = MARGIN, MARGIN
        bank_pos = doc.text(x, y, 120, 8, stmt.bank.name, size=15, bold=True)
        if first:
            field("institution", stmt.bank.name, bank_pos)
        if key == "th":
            doc.text(x, y + 7, 120, 5, stmt.bank.name_en, size=9, font="latin")
        if key == "de_saldo":
            doc.text(x, y + 7, 120, 5, "Rechtsnachfolgerin der Rheinauen Privatbank", size=8, grey=True)
        doc.text(x, y + 12, 120, 5, stmt.bank.address, size=8, grey=True)
        title, _, subtitle = lay.title.partition(" / ")
        doc.text(PAGE_W - MARGIN - 70, y, 70, 8, title, size=11, bold=True, align="R")
        if subtitle:
            doc.text(PAGE_W - MARGIN - 70, y + 7, 70, 5, subtitle, size=8.5, align="R", font="latin")
        if key.startswith("de"):
            doc.text(PAGE_W - MARGIN - 70, y + 7, 70, 5, f"Nr. {stmt.month}/{stmt.year}", size=9, align="R")
        y += 22
        labels = {
            "kr": ("예금주", "계좌번호", "상품명", "조회기간", "통화"),
            "de_plain": ("Kontoinhaber", "IBAN", "Kontoart", "Zeitraum", "Währung"),
            "de_saldo": ("Kontoinhaber", "IBAN", "Kontoart", "Zeitraum", "Währung"),
            "th": ("ชื่อบัญชี", "เลขที่บัญชี", "ประเภทบัญชี", "รอบรายการ", "สกุลเงิน"),
        }[key]
        period = (
            f"{fmt_date(start, key)} ~ {fmt_date(end, key)}"
            if key == "kr"
            else f"{fmt_date(start, key)} - {fmt_date(end, key)}"
        )
        values = (spec.holder_printed, spec.account_number, spec.product, period, cur)
        names = ("holder", "account_number", "product", "period", "currency")
        rows = list(zip(labels, values, names, strict=True))
        if not first:
            rows = rows[1:2]
        for label, value, name in rows:
            doc.text(x, y, 32, 5.5, label, size=8.5, grey=True)
            pos = doc.text(
                x + 32,
                y,
                110,
                5.5,
                value,
                size=9.5,
                bold=name == "account_number",
                font="latin" if name == "account_number" else None,
            )
            if first:
                field(name, value, pos)
            y += 6
        if key.startswith("de") and first and stmt.bank.bic:
            doc.text(x, y, 32, 5.5, "BIC", size=8.5, grey=True)
            doc.text(x + 32, y, 60, 5.5, stmt.bank.bic, size=9.5, font="latin")
            y += 6
        return y + 3

    def table_header(y: float) -> float:
        x = MARGIN
        doc.pdf.set_fill_color(235)
        doc.pdf.rect(x, y, sum(c.width for c in lay.columns), ROW_H, style="F")
        for c in lay.columns:
            doc.text(x, y, c.width, ROW_H, c.label, size=8.5, bold=True, align=c.align)
            x += c.width
        return y + ROW_H

    y = header(first=True)

    # Opening balance, in each layout's own place.
    if key == "kr":
        doc.text(MARGIN, y, 40, 6, "전월이월 잔액", size=9, grey=True)
        field(
            "opening_balance",
            stmt.opening_balance,
            doc.text(
                MARGIN + 40,
                y,
                45,
                6,
                money(stmt.opening_balance),
                size=10,
                bold=True,
                align="R",
                font="latin",
            ),
        )
        credits = sum(t.amount for t in stmt.txns if t.amount > 0)
        debits = -sum(t.amount for t in stmt.txns if t.amount < 0)
        doc.text(MARGIN + 95, y, 25, 6, "입금 합계", size=9, grey=True)
        doc.text(MARGIN + 120, y, 30, 6, money(credits), size=9, align="R", font="latin")
        y += 6
        doc.text(MARGIN + 95, y, 25, 6, "출금 합계", size=9, grey=True)
        doc.text(MARGIN + 120, y, 30, 6, money(debits), size=9, align="R", font="latin")
        y += 9
    elif key.startswith("de"):
        prev = start - timedelta(days=1)
        doc.text(MARGIN, y, 70, 6, f"Alter Kontostand vom {fmt_date(prev, key)}", size=9)
        field(
            "opening_balance",
            stmt.opening_balance,
            doc.text(
                MARGIN + 70, y, 40, 6, f"{money(stmt.opening_balance)} EUR", size=10, bold=True, align="R"
            ),
        )
        y += 9

    y = table_header(y)
    lines: dict[str, dict] = {}

    if key == "th":  # brought-forward row
        cols = {c.key: c for c in lay.columns}
        x_bal = MARGIN + sum(c.width for c in lay.columns if c.key != "balance")
        doc.text(MARGIN + cols["date"].width, y, cols["desc"].width, ROW_H, "ยอดยกมา (BALANCE B/F)", size=8.5)
        field(
            "opening_balance",
            stmt.opening_balance,
            doc.text(
                x_bal,
                y,
                cols["balance"].width,
                ROW_H,
                money(stmt.opening_balance),
                size=8.5,
                align="R",
                font="latin",
            ),
        )
        y += ROW_H

    def draw_row(i: int, t: Txn, y: float):
        x = MARGIN
        rec = {
            "page": doc.page,
            "row_bbox": box(MARGIN, y, sum(c.width for c in lay.columns), ROW_H),
            "amount_bbox": None,
            "amount_text_bbox": None,
            "balance_bbox": None,
            "balance_text_bbox": None,
        }
        for c in lay.columns:
            val, font = "", "latin"
            if c.key == "no":
                val = str(i)
            elif c.key in ("date", "value"):
                val = (
                    fmt_date(t.day, key)
                    if key != "de_plain" or c.key == "value"
                    else t.day.strftime("%d.%m.")
                )
            elif c.key == "desc":
                val, font = t.description, lay.font
            elif c.key == "amount":
                val = money(t.amount)
            elif c.key == "debit" and t.amount < 0:
                val = money(-t.amount)
            elif c.key == "credit" and t.amount > 0:
                val = money(t.amount)
            elif c.key == "balance":
                val = money(t.balance_after)
            pos = doc.text(x, y, c.width, ROW_H, val, size=8.5, align=c.align, font=font)
            if val and c.key in ("amount", "debit", "credit"):
                rec["amount_bbox"], rec["amount_text_bbox"] = pos["bbox"], pos["text_bbox"]
            if c.key == "balance":
                rec["balance_bbox"], rec["balance_text_bbox"] = pos["bbox"], pos["text_bbox"]
            x += c.width
        doc.pdf.set_draw_color(225)
        doc.pdf.line(MARGIN, y + ROW_H, MARGIN + sum(c.width for c in lay.columns), y + ROW_H)
        lines[t.txn_id] = rec

    for i, t in enumerate(stmt.txns, start=1):
        if y + ROW_H > BOTTOM - 14:
            y = table_header(header(first=False))
        draw_row(i, t, y)
        y += ROW_H

    # Closing balance.
    y += 3
    if key == "kr":
        doc.text(MARGIN + 95, y, 25, 6, "기말 잔액", size=9, grey=True)
        field(
            "closing_balance",
            stmt.closing_balance,
            doc.text(
                MARGIN + 120,
                y,
                30,
                6,
                money(stmt.closing_balance),
                size=10,
                bold=True,
                align="R",
                font="latin",
            ),
        )
    elif key.startswith("de"):
        doc.text(MARGIN, y, 70, 6, f"Neuer Kontostand vom {fmt_date(end, key)}", size=9)
        field(
            "closing_balance",
            stmt.closing_balance,
            doc.text(
                MARGIN + 70, y, 40, 6, f"{money(stmt.closing_balance)} EUR", size=10, bold=True, align="R"
            ),
        )
    else:
        doc.text(MARGIN, y, 60, 6, "ยอดยกไป (BALANCE C/F)", size=9, grey=True)
        field(
            "closing_balance",
            stmt.closing_balance,
            doc.text(
                MARGIN + 60,
                y,
                40,
                6,
                money(stmt.closing_balance),
                size=10,
                bold=True,
                align="R",
                font="latin",
            ),
        )

    pages = doc.page
    for p in range(1, pages + 1):
        doc.pdf.page = p
        doc.text(MARGIN, PAGE_H - 14, 150, 5, FOOTER, size=7, font="latin", grey=True)
        doc.text(
            PAGE_W - MARGIN - 20,
            PAGE_H - 14,
            20,
            5,
            f"{p}/{pages}",
            size=7,
            font="latin",
            grey=True,
            align="R",
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.pdf.output(str(out))
    return {
        "pages": pages,
        "prints_running_balance": lay.prints_running_balance,
        "fields": fields,
        "lines": lines,
    }
