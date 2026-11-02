"""Snap model row boxes onto the printed lines of the page image.

The step-1 probe showed muse-spark-1.3 places boxes almost exactly across the
page but drifts upward down it (3 grid units at row 1, 12 at row 12, about one
text line). This finds printed lines from the image alone (no answer key):

1. Ink bands: horizontal runs of dark pixels inside the table's x-range.
2. Alignment: rows come back in printed order, so they map onto a contiguous
   run of bands. The model's x positions are reliable even when its y drifts,
   so the strongest signal is each row's amount column (debit or credit): on
   the true line that column has ink and the other is empty, which the table
   header and shifted runs break. A run where every row matches, and no other
   run does, is accepted.
3. Fallback when the column pattern can't decide (one-column layouts, all
   rows in one column): the least-squares residual plus the first row's
   displacement, and only if it beats the runner-up by MIN_MARGIN grid units.
   Otherwise don't snap; a wrong snap is worse than a loose box.

Each row's y-extent is replaced by its band's; x is kept. Returns None when no
confident alignment exists, so callers fall back to the raw boxes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PIL import Image

from .page import FIELD_NAMES

INK_THRESHOLD = 160  # 0-255; the light grey row rules (225) don't count as ink
MAX_SCALE_DEVIATION = 0.35  # observed row-spacing compression: 8% (Oct 27 run 1), 16% (run 3)
MAX_PATTERN_SHIFT = 45.0  # grid units; about two printed lines
MIN_MARGIN = 3.0  # grid units between best and runner-up alignment cost


def ink_bands(image: Path, x_min: int, x_max: int, min_height_px: int = 3) -> list[tuple[int, int]]:
    """Vertical extents (0-1000 grid) of text lines inside [x_min, x_max] on the 0-1000 grid."""
    img = Image.open(image).convert("L")
    w, h = img.size
    x0, x1 = max(0, x_min * w // 1000), min(w, max(x_min + 1, x_max) * w // 1000)
    region = img.crop((x0, 0, x1, h))
    pixels = region.load()
    rw = region.size[0]
    inked = [any(pixels[x, y] < INK_THRESHOLD for x in range(0, rw, 2)) for y in range(h)]
    bands, start = [], None
    for y, on in enumerate(inked + [False]):
        if on and start is None:
            start = y
        elif not on and start is not None:
            if y - start >= min_height_px:
                bands.append((start, y))
            start = None
    return [(round(a * 1000 / h), round(b * 1000 / h)) for a, b in bands]


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    n = len(xs)
    if n == 1:
        return 1.0, ys[0] - xs[0], 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx if sxx else 1.0
    b = my - a * mx
    rms = (sum((a * x + b - y) ** 2 for x, y in zip(xs, ys, strict=True)) / n) ** 0.5
    return a, b, rms


def align_rows(
    row_centres: list[float], bands: list[tuple[int, int]], pattern: Callable[[int], int] | None = None
) -> tuple[list[int], float] | None:
    """(band index for each row, confidence margin), or None if no run fits confidently.

    pattern(s), if given, counts the rows whose ink pattern matches when row 0 sits on band s.
    """
    k = len(row_centres)
    if k == 0 or len(bands) < k:
        return None
    starts = range(len(bands) - k + 1)
    if pattern is not None:
        matches = sorted(((pattern(s), s) for s in starts), reverse=True)
        best, runner_up = matches[0], matches[1] if len(matches) > 1 else (-1, None)
        if best[0] == k and runner_up[0] < k and _near(row_centres, bands, best[1]):
            return [best[1] + i for i in range(k)], float(best[0] - runner_up[0])
    centres = [(a + b) / 2 for a, b in bands]
    costs = []
    for s in starts:
        a, _, rms = _fit(row_centres, centres[s : s + k])
        if k > 1 and abs(a - 1) > MAX_SCALE_DEVIATION:
            continue
        costs.append((rms + abs(row_centres[0] - centres[s]), s))
    if not costs:
        return None
    costs.sort()
    margin = costs[1][0] - costs[0][0] if len(costs) > 1 else float("inf")
    if margin < MIN_MARGIN or not _near(row_centres, bands, costs[0][1]):
        return None
    return [costs[0][1] + i for i in range(k)], margin


def _near(row_centres: list[float], bands: list[tuple[int, int]], start: int) -> bool:
    """A pattern match must also sit roughly where the model put the rows (guards a skipped row)."""
    centres = [(a + b) / 2 for a, b in bands[start : start + len(row_centres)]]
    shift = sum(abs(c - r) for c, r in zip(centres, row_centres, strict=True)) / len(row_centres)
    return shift <= MAX_PATTERN_SHIFT


def _column_pattern(
    rows: list[dict], bands: list[tuple[int, int]], img: Image.Image
) -> Callable[[int], int] | None:
    """Score a start band by how many rows show ink in their own amount column and none in the other."""
    cols: dict[str, tuple[int, int]] = {}
    for r in rows:
        b, c = r["amount_bbox"], r["amount_column"]
        if c in ("debit", "credit") and any(b.values()):
            lo, hi = cols.get(c, (b["x_min"], b["x_max"]))
            cols[c] = (min(lo, b["x_min"]), max(hi, b["x_max"]))
    if len(cols) < 2:
        return None  # one column only: the pattern can't tell the header from a row
    (d0, d1), (c0, c1) = cols["debit"], cols["credit"]
    if d0 < c1 and c0 < d1:
        return None  # in and out share one amount column (direction printed elsewhere): no pattern
    w, h = img.size
    pixels = img.load()

    def inked(band: tuple[int, int], xr: tuple[int, int]) -> bool:
        y0 = min(h - 1, band[0] * h // 1000)
        y1 = min(h, max(y0 + 1, band[1] * h // 1000))
        x0 = min(w - 1, xr[0] * w // 1000)
        x1 = min(w, max(x0 + 1, xr[1] * w // 1000))
        return any(pixels[x, y] < INK_THRESHOLD for y in range(y0, y1) for x in range(x0, x1, 2))

    def score(s: int) -> int:
        ok = 0
        for i, r in enumerate(rows):
            own = r["amount_column"]
            if own not in cols:
                continue
            other = "credit" if own == "debit" else "debit"
            ok += inked(bands[s + i], cols[own]) and not inked(bands[s + i], cols[other])
        return ok

    return score


def snap_rows(reading: dict, image: Path) -> dict | None:
    """A copy of the reading with row, amount and balance boxes snapped to printed lines."""
    rows = reading.get("rows") or []
    if not rows:
        return None
    x_min = min(r["row_bbox"]["x_min"] for r in rows)
    x_max = max(r["row_bbox"]["x_max"] for r in rows)
    bands = ink_bands(image, x_min, x_max)
    centres = [(r["row_bbox"]["y_min"] + r["row_bbox"]["y_max"]) / 2 for r in rows]
    pattern = _column_pattern(rows, bands, Image.open(image).convert("L"))
    aligned = align_rows(centres, bands, pattern)
    if aligned is None:
        return None
    idx, margin = aligned
    snapped = {**reading, "rows": [], "snap_margin": round(margin, 1)}
    for r, j in zip(rows, idx, strict=True):
        y0, y1 = bands[j]
        new = dict(r)
        for key in ("row_bbox", "amount_bbox", "balance_bbox"):
            b = r[key]
            if any(b.values()):
                new[key] = {**b, "y_min": y0, "y_max": y1}
        snapped["rows"].append(new)
    return snapped


FIELD_REACH = 25  # grid units a header field's box may be from its printed line; about one line
FIELD_MIN_X_OVERLAP = 0.6  # overlap between the box's columns and a line's ink; a column heading is narrower
FIELD_MIN_MARGIN = 6  # grid units between the nearest matching line and the next; closer than this, abstain
_FIELD_X_PAD = 25  # grid units of slack on each side when looking for a line's ink


def _x_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    union = max(a1, b1) - min(a0, b0)
    return max(0, min(a1, b1) - max(a0, b0)) / union if union else 0


def _ink_lines(mask: Image.Image, x_min: int, x_max: int) -> list[tuple[int, int, int, int]]:
    """(y0, y1, x0, x1) on the 0-1000 grid for each text line in the columns around [x_min, x_max]."""
    w, h = mask.size
    left = max(0, (x_min - _FIELD_X_PAD) * w // 1000)
    right = min(w, (x_max + _FIELD_X_PAD) * w // 1000)
    if right <= left:
        return []
    region = mask.crop((left, 0, right, h))
    lines, start = [], None
    for y, on in enumerate([*region.getprojection()[1], 0]):
        if on and start is None:
            start = y
        elif not on and start is not None:
            if y - start >= 3:
                box = region.crop((0, start, right - left, y)).getbbox()
                if box:
                    lines.append(
                        (
                            round(start * 1000 / h),
                            round(y * 1000 / h),
                            round((left + box[0]) * 1000 / w),
                            round((left + box[2]) * 1000 / w),
                        )
                    )
            start = None
    return lines


def snap_field(box: dict, mask: Image.Image) -> dict | None:
    """The printed line a header field's box points to, or None when the image doesn't settle it.

    The model's columns stay accurate while its height drifts, as with rows, so a line must fill about the
    same columns as the box. That rejects a narrower column heading sitting closer than the value itself.
    """
    centre = (box["y_min"] + box["y_max"]) / 2
    matches = sorted(
        (abs((y0 + y1) / 2 - centre), y0, y1, x0, x1)
        for y0, y1, x0, x1 in _ink_lines(mask, box["x_min"], box["x_max"])
        if abs((y0 + y1) / 2 - centre) <= FIELD_REACH
        and _x_overlap(box["x_min"], box["x_max"], x0, x1) >= FIELD_MIN_X_OVERLAP
    )
    if not matches or (len(matches) > 1 and matches[1][0] - matches[0][0] < FIELD_MIN_MARGIN):
        return None
    _, y0, y1, x0, x1 = matches[0]
    return {"x_min": x0, "y_min": y0, "x_max": x1, "y_max": y1}


def snap_fields(reading: dict, image: Path) -> dict[str, dict | None]:
    """Each header field's snapped box. None means no highlight, rather than a box on the wrong line."""
    gray = Image.open(image).convert("L")
    mask = gray.point(lambda v: 255 if v < INK_THRESHOLD else 0)
    out = {}
    for name in FIELD_NAMES:
        field = reading.get(name) or {}
        box = field.get("bbox")
        out[name] = snap_field(box, mask) if box and any(box.values()) and field.get("text") else None
    return out


def snap_reading(reading: dict, image: Path) -> dict:
    """A copy of the reading with every box that can be placed on its printed line."""
    snapped = snap_rows(reading, image) or dict(reading)
    for name, box in snap_fields(reading, image).items():
        snapped[name] = {**reading[name], "bbox": box}
    return snapped
