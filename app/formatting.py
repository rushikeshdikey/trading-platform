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


def register(env) -> None:
    env.filters["inr"] = inr
    env.filters["inr_signed"] = inr_signed
    env.filters["pct"] = pct
    env.filters["pct_signed"] = pct_signed
    env.filters["num"] = num
    env.filters["dtfmt"] = dtfmt
    env.filters["shortdate"] = shortdate
    env.filters["pnl_color"] = pnl_color
