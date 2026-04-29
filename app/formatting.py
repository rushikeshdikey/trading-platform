"""Jinja filters for pretty INR / % / date formatting."""
from __future__ import annotations

from datetime import date, datetime


def inr(value) -> str:
    """Indian-style grouping: 1,00,000 not 100,000."""
    if value is None or value == "":
        return ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if n < 0 else ""
    # Round the full value first, then split into whole + paise. Splitting
    # first causes -549.99999 → whole=549, frac=1.0 → ".100" (3-digit bug).
    n = round(abs(n), 2)
    whole = int(n)
    paise = int(round((n - whole) * 100))
    s = str(whole)
    if len(s) > 3:
        last3, rest = s[-3:], s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        out = ",".join(groups) + "," + last3
    else:
        out = s
    if paise:
        out += f".{paise:02d}"
    return f"{sign}₹{out}"


def inr_signed(value) -> str:
    s = inr(value)
    try:
        n = float(value)
    except (TypeError, ValueError):
        return s
    if n > 0 and not s.startswith("-"):
        return "+" + s
    return s


def pct(value, digits: int = 2) -> str:
    if value is None or value == "":
        return ""
    try:
        n = float(value) * 100
    except (TypeError, ValueError):
        return str(value)
    return f"{n:.{digits}f}%"


def pct_signed(value, digits: int = 2) -> str:
    if value is None or value == "":
        return ""
    try:
        n = float(value) * 100
    except (TypeError, ValueError):
        return str(value)
    sign = "+" if n > 0 else ""
    return f"{sign}{n:.{digits}f}%"


def num(value, digits: int = 2) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def dtfmt(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y")
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return str(value) if value is not None else ""


def shortdate(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d %b")
    if isinstance(value, date):
        return value.strftime("%d %b")
    return str(value) if value is not None else ""


def pnl_color(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "text-slate-700"
    if n > 0:
        return "text-emerald-600"
    if n < 0:
        return "text-rose-600"
    return "text-slate-700"


# TradingView chart URL helper.
#
# Most Indian swing-tradeable stocks are dual-listed (NSE + BSE) with NSE as
# the higher-volume venue, so NSE: prefix is the safe default. Stocks that
# ONLY list on BSE need BSE: prefix or TradingView shows "symbol not found".
#
# We maintain a small hand-curated set in `data/bse_only_symbols.csv`. Reload
# checks the file's mtime so editing the CSV during dev takes effect on next
# request without restart.

from pathlib import Path as _Path
import csv as _csv
import logging as _logging

_log = _logging.getLogger("journal.formatting")
_BSE_ONLY_FILE = _Path(__file__).resolve().parent.parent / "data" / "bse_only_symbols.csv"
_bse_only_cache: set[str] | None = None
_bse_only_mtime: float = 0.0


def _load_bse_only() -> set[str]:
    """Load BSE-only symbol set. Re-reads when file mtime changes — supports
    live edit during dev. Returns an empty set on missing file."""
    global _bse_only_cache, _bse_only_mtime
    if not _BSE_ONLY_FILE.exists():
        return set()
    mtime = _BSE_ONLY_FILE.stat().st_mtime
    if _bse_only_cache is not None and mtime == _bse_only_mtime:
        return _bse_only_cache
    out: set[str] = set()
    try:
        with _BSE_ONLY_FILE.open() as f:
            for row in _csv.reader(f):
                if not row:
                    continue
                v = row[0].strip()
                if not v or v.startswith("#") or v.lower() == "symbol":
                    continue
                out.add(v.upper())
    except Exception as exc:  # noqa: BLE001
        _log.warning("bse_only_symbols load failed: %s", exc)
        return _bse_only_cache or set()
    _bse_only_cache = out
    _bse_only_mtime = mtime
    return out


def tv_url(symbol) -> str:
    """TradingView chart URL for an Indian-listed equity.

    Default exchange is NSE; BSE-only symbols (per bse_only_symbols.csv) get
    BSE: prefix so the chart actually resolves.
    """
    if not symbol:
        return "https://www.tradingview.com/"
    s = str(symbol).strip().upper()
    exchange = "BSE" if s in _load_bse_only() else "NSE"
    return f"https://www.tradingview.com/chart/?symbol={exchange}%3A{s}"


def register(env) -> None:
    env.filters["inr"] = inr
    env.filters["inr_signed"] = inr_signed
    env.filters["pct"] = pct
    env.filters["pct_signed"] = pct_signed
    env.filters["num"] = num
    env.filters["dtfmt"] = dtfmt
    env.filters["shortdate"] = shortdate
    env.filters["pnl_color"] = pnl_color
    env.filters["tv_url"] = tv_url
