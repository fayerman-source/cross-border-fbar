"""Snap model row boxes onto the printed lines of the page image.

The step-1 probe showed muse-spark-1.3 places boxes almost exactly across the
page but drifts upward down it (3 grid units at row 1, 12 at row 12, about one
text line). This finds printed lines from the image alone (no answer key):

1. Ink bands: horizontal runs of dark pixels inside the table's x-range.
2. Alignment: rows come back in printed order, so they map onto a contiguous
   run of bands. Evenly spaced bands make every shifted run fit the *shape*
   equally well, so each run's cost is the least-squares residual plus the
   first row's displacement, where the model drifts least.
3. Confidence: if the best run doesn't beat the runner-up by MIN_MARGIN grid
   units, don't snap; a wrong snap is worse than a loose box.

Each row's y-extent is replaced by its band's; x is kept. Returns None when no
confident alignment exists, so callers fall back to the raw boxes.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

INK_THRESHOLD = 160  # 0-255; the light grey row rules (225) don't count as ink
MAX_SCALE_DEVIATION = 0.15
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


def align_rows(row_centres: list[float], bands: list[tuple[int, int]]) -> tuple[list[int], float] | None:
    """(band index for each row, margin over the runner-up), or None if no run fits confidently."""
    k = len(row_centres)
    if k == 0 or len(bands) < k:
        return None
    centres = [(a + b) / 2 for a, b in bands]
    costs = []
    for s in range(len(bands) - k + 1):
        a, _, rms = _fit(row_centres, centres[s : s + k])
        if k > 1 and abs(a - 1) > MAX_SCALE_DEVIATION:
            continue
        costs.append((rms + abs(row_centres[0] - centres[s]), s))
    if not costs:
        return None
    costs.sort()
    margin = costs[1][0] - costs[0][0] if len(costs) > 1 else float("inf")
    if margin < MIN_MARGIN:
        return None
    return [costs[0][1] + i for i in range(k)], margin


def snap_rows(reading: dict, image: Path) -> dict | None:
    """A copy of the reading with row, amount and balance boxes snapped to printed lines."""
    rows = reading.get("rows") or []
    if not rows:
        return None
    x_min = min(r["row_bbox"]["x_min"] for r in rows)
    x_max = max(r["row_bbox"]["x_max"] for r in rows)
    bands = ink_bands(image, x_min, x_max)
    centres = [(r["row_bbox"]["y_min"] + r["row_bbox"]["y_max"]) / 2 for r in rows]
    aligned = align_rows(centres, bands)
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
