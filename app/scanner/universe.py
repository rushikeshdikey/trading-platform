"""Scanner symbol universe.

Sourced from two places, merged:
1. Cached ``DailyBar`` symbols — anything seen in bhavcopy in the lookback
   window. This is the authoritative 'tradeable on NSE today' set.
2. ``KiteInstrument`` NSE equity rows — covers SME and anything bhavcopy
   hasn't yet surfaced.

Returns uppercased tradingsymbols, deduped.

**ETFs and debt/liquid funds are excluded.** On NSE they trade under the same
``EQ`` series + ``instrument_type='EQ'`` as stocks, so the only reliable
signals are (a) symbol suffix (``BEES``/``ETF``/``IETF``) and (b) tokens in
the Kite instrument name (``ETF``, ``LIQUID``, ``INDEX FUND``, etc.). Letting
them into the universe pollutes the Tight Setup scan in particular because
these products are engineered to have near-zero volatility.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..models import DailyBar, KiteInstrument


# Symbol-level markers. Case-insensitive. Any match → exclude.
_ETF_SYMBOL_RE = re.compile(r"(BEES|IETF|ETF)", re.IGNORECASE)

# Kite instrument-name tokens. Case-insensitive substring match.
_FUND_NAME_TOKENS = (
    "ETF",
    "EXCHANGE TRADED",
    "INDEX FUND",
    "LIQUID FUND",
    "LIQUID RATE",
    "LIQUID SCHEME",
    "GOLD FUND",
    "GILT FUND",
    "DEBT FUND",
    "MUTUAL FUND",
    "MONEY MARKET",
    "OVERNIGHT FUND",
    "G-SEC",
    "BHARAT BOND",
)


def _is_etf_or_fund(symbol: str, name: str | None) -> bool:
    """True if this tradingsymbol looks like an ETF / debt fund, not a stock."""
    sym = (symbol or "").upper()
    if _ETF_SYMBOL_RE.search(sym):
        return True
    nm = (name or "").upper()
    return any(tok in nm for tok in _FUND_NAME_TOKENS)


def _kite_name_by_symbol(db: Session) -> dict[str, str]:
    """Lookup table: NSE tradingsymbol → name (for ETF-detection)."""
    rows = (
        db.query(KiteInstrument.tradingsymbol, KiteInstrument.name)
        .filter(KiteInstrument.exchange == "NSE")
        .all()
    )
    return {(sym or "").upper(): (name or "") for sym, name in rows}


def nse_equity_universe(db: Session) -> list[str]:
    """All NSE equity symbols we can scan — bhavcopy cache ∪ Kite EQ master,
    minus anything that looks like an ETF / liquid / debt fund."""
    bhav_syms = {s for (s,) in db.query(DailyBar.symbol).distinct().all() if s}
    kite_syms = {
        s
        for (s,) in db.query(KiteInstrument.tradingsymbol)
        .filter(KiteInstrument.exchange == "NSE", KiteInstrument.instrument_type == "EQ")
        .all()
        if s
    }
    merged = {s.upper() for s in (bhav_syms | kite_syms)}
    names = _kite_name_by_symbol(db)
    return sorted(s for s in merged if not _is_etf_or_fund(s, names.get(s)))


def universe_from_cache(db: Session) -> list[str]:
    """Cached bar symbols minus ETFs / debt-fund products."""
    cache_syms = {s.upper() for (s,) in db.query(DailyBar.symbol).distinct().all() if s}
    names = _kite_name_by_symbol(db)
    return sorted(s for s in cache_syms if not _is_etf_or_fund(s, names.get(s)))
