"""Server-side SVG sparklines for scanner result rows.

Why server-side: each sparkline is ~120 bytes of SVG. For 100 results
that's 12 KB of HTML — dwarfed by the rest of the page. A JS chart lib
would be 200 KB+ and a hundred Canvas inits per page-render. SVG inline
ships with the HTML, paints instantly, no JS needed.

The rendering pipeline:

1. ``bulk_sparklines(db, symbols, lookback)`` issues ONE query that
   returns the last ``lookback`` bars for each symbol, builds a path,
   returns ``{symbol: <svg>...</svg>}``.

2. Each SVG is sized 64×18 pixels: a tiny line chart of closes plus a
   colored direction marker. The line is 2px stroke; if the close moved
   up over the lookback, color is emerald, else rose.

We don't pre-compute these into the DB — the bars cache changes daily
and the cost is ~10ms for 100 symbols.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date as _date, timedelta

from sqlalchemy.orm import Session

from ..models import DailyBar

# Sparkline visual tuning. Width and height are picked to fit nicely
# inside a table cell without overpowering the row.
W = 64
H = 18
PAD_X = 1
PAD_Y = 2

GREEN = "#059669"  # emerald-600
RED = "#e11d48"    # rose-600
GREY = "#a1a1aa"   # zinc-400


def bulk_sparklines(
    db: Session, symbols: list[str], lookback: int = 30,
) -> dict[str, str]:
    """Return ``{symbol: svg-string}`` for the given symbols.

    One DB query for the whole batch. Symbols with fewer than 2 bars get
    an empty string (caller renders ``—``).
    """
    if not symbols:
        return {}
    sym_set = {s.upper() for s in symbols}

    # We want the last `lookback` bars per symbol. SQLite + Postgres differ
    # on window function support; just pull a wider date range and trim
    # in Python — bars are tiny.
    cutoff = _date.today() - timedelta(days=lookback * 2 + 14)
    rows = (
        db.query(DailyBar.symbol, DailyBar.date, DailyBar.close)
        .filter(DailyBar.symbol.in_(sym_set), DailyBar.date >= cutoff)
        .order_by(DailyBar.symbol.asc(), DailyBar.date.asc())
        .all()
    )
    by_sym: dict[str, list[float]] = defaultdict(list)
    for sym, _d, close in rows:
        by_sym[sym].append(close)

    out: dict[str, str] = {}
    for sym in sym_set:
        closes = by_sym.get(sym, [])[-lookback:]
        if len(closes) < 2:
            out[sym] = ""
            continue
        out[sym] = _build_svg(closes)
    return out


def _build_svg(closes: list[float]) -> str:
    """Linear sparkline from a list of closes."""
    n = len(closes)
    lo = min(closes)
    hi = max(closes)
    span = hi - lo
    color = GREEN if closes[-1] >= closes[0] else RED
    if span < 1e-9:
        # Flat line — degenerate but legal.
        y_mid = H / 2
        path = f"M{PAD_X},{y_mid:.1f} L{W - PAD_X},{y_mid:.1f}"
        return _wrap(path, GREY, n)

    plot_w = W - 2 * PAD_X
    plot_h = H - 2 * PAD_Y
    pts: list[str] = []
    for i, c in enumerate(closes):
        x = PAD_X + (i / (n - 1)) * plot_w
        y = PAD_Y + (1 - (c - lo) / span) * plot_h  # invert: hi at top
        pts.append(f"{x:.1f},{y:.1f}")
    path = "M" + " L".join(pts)
    return _wrap(path, color, n)


def _wrap(path: str, color: str, n_bars: int) -> str:
    title = f"Last {n_bars} bars"
    return (
        f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{title}">'
        f'<title>{title}</title>'
        f'<path d="{path}" fill="none" stroke="{color}" '
        f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )
