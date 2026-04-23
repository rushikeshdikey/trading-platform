"""Zerodha Kite Connect integration.

Handles: OAuth-style login (request_token → access_token), daily token
persistence in the Setting table, LTP lookup, and instrument master download.

Kite's access_token is valid until ~06:00 IST the next day. We store it with
the date it was obtained; anything not dated today is treated as expired.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from . import config
from . import settings as app_settings

log = logging.getLogger("journal.kite")

IST = timezone(timedelta(hours=5, minutes=30))
_TOKEN_KEY = "kite_access_token"
_TOKEN_DATE_KEY = "kite_access_token_date"
_USER_ID_KEY = "kite_user_id"
_USER_NAME_KEY = "kite_user_name"


def _today_ist() -> date:
    """Kite rolls tokens at 06:00 IST — treat anything before that as "still yesterday"."""
    now = datetime.now(tz=IST)
    if now.time() < dtime(6, 0):
        return (now - timedelta(days=1)).date()
    return now.date()


def is_authed(db: Session) -> bool:
    token = app_settings.get(db, _TOKEN_KEY)
    token_date = app_settings.get(db, _TOKEN_DATE_KEY)
    if not token or not token_date:
        return False
    try:
        d = datetime.strptime(token_date, "%Y-%m-%d").date()
    except ValueError:
        return False
    return d >= _today_ist()


def auth_status(db: Session) -> dict[str, Any]:
    return {
        "configured": config.kite_configured(),
        "authed": is_authed(db),
        "user_id": app_settings.get(db, _USER_ID_KEY),
        "user_name": app_settings.get(db, _USER_NAME_KEY),
        "token_date": app_settings.get(db, _TOKEN_DATE_KEY),
    }


def login_url() -> str:
    """URL to send the user to for the Kite login flow."""
    if not config.KITE_API_KEY:
        raise RuntimeError("KITE_API_KEY not configured")
    from kiteconnect import KiteConnect

    kc = KiteConnect(api_key=config.KITE_API_KEY)
    return kc.login_url()


def exchange_request_token(db: Session, request_token: str) -> dict[str, Any]:
    """Swap a ``request_token`` from the callback for an ``access_token`` and persist."""
    if not (config.KITE_API_KEY and config.KITE_API_SECRET):
        raise RuntimeError("KITE_API_KEY / KITE_API_SECRET not configured")
    from kiteconnect import KiteConnect

    kc = KiteConnect(api_key=config.KITE_API_KEY)
    data = kc.generate_session(request_token, api_secret=config.KITE_API_SECRET)
    access_token = data["access_token"]
    app_settings.set_value(db, _TOKEN_KEY, access_token)
    app_settings.set_value(db, _TOKEN_DATE_KEY, _today_ist().isoformat())
    if data.get("user_id"):
        app_settings.set_value(db, _USER_ID_KEY, str(data["user_id"]))
    if data.get("user_name"):
        app_settings.set_value(db, _USER_NAME_KEY, str(data["user_name"]))
    db.commit()
    return data


def logout(db: Session) -> None:
    for k in (_TOKEN_KEY, _TOKEN_DATE_KEY, _USER_ID_KEY, _USER_NAME_KEY):
        app_settings.set_value(db, k, "")
    db.commit()


def client(db: Session):
    """Return an authenticated KiteConnect client, or None if not authed."""
    if not is_authed(db):
        return None
    from kiteconnect import KiteConnect

    kc = KiteConnect(api_key=config.KITE_API_KEY)
    kc.set_access_token(app_settings.get(db, _TOKEN_KEY))
    return kc


# -- LTP ---------------------------------------------------------------------


def ltp(db: Session, symbols: list[str]) -> dict[str, float]:
    """Fetch last traded prices for a list of journal symbols.

    Looks up each symbol in the cached instrument master to resolve the right
    ``EXCHANGE:TRADINGSYMBOL`` pair (handles NSE/BSE/NSE-SME/BSE-SME uniformly)
    and issues a single batched ``kite.ltp()`` call.

    Returns a dict mapping journal-symbol → price. Missing symbols are omitted.
    """
    from .models import KiteInstrument  # local import to avoid cycles

    kc = client(db)
    if kc is None or not symbols:
        return {}

    out: dict[str, float] = {}
    kite_keys: dict[str, str] = {}  # journal_symbol -> "EXCH:TRADINGSYMBOL"

    for sym in symbols:
        inst = _resolve_instrument(db, sym)
        if inst is None:
            continue
        kite_keys[sym] = f"{inst.exchange}:{inst.tradingsymbol}"

    if not kite_keys:
        return out

    try:
        resp = kc.ltp(list(kite_keys.values()))
    except Exception as e:  # noqa: BLE001
        log.warning("kite ltp() failed: %s", e)
        return out

    for journal_sym, kite_key in kite_keys.items():
        entry = resp.get(kite_key)
        if not entry:
            continue
        price = entry.get("last_price")
        try:
            out[journal_sym] = float(price)
        except (TypeError, ValueError):
            continue
    return out


def _clean_symbol(symbol: str) -> str:
    """Drop broker-export artefacts that aren't part of the actual ticker.

    Strips trailing ``.0`` (BSE numeric scrip codes parsed as floats) and
    series suffixes like ``-EQ`` / ``-BE`` / ``-BZ`` that Zerodha appends.
    Does NOT strip ``-SM`` because that IS part of the tradingsymbol for NSE
    SME listings (e.g. ``SKP-SM``).
    """
    s = (symbol or "").strip().upper()
    if "." in s:
        base, _, tail = s.rpartition(".")
        if tail in {"0", "00"} and base:
            s = base
    if "-" in s:
        base, _, tail = s.partition("-")
        if tail in {"EQ", "BE", "BZ"}:
            s = base
    return s


def _symbol_candidates(symbol: str) -> list[str]:
    """Resolution order: literal, cleaned, then the literal with ``-SM``
    appended so journal entries like ``ALPEXSOLAR`` still hit their NSE SME
    row ``ALPEXSOLAR-SM``."""
    literal = (symbol or "").strip().upper()
    out: list[str] = [literal]
    cleaned = _clean_symbol(literal)
    if cleaned and cleaned not in out:
        out.append(cleaned)
    if literal and "-" not in literal and not literal.isdigit():
        out.append(f"{literal}-SM")
    return out


def _resolve_instrument(db: Session, symbol: str):
    """Best-effort lookup in KiteInstrument for a journal symbol.

    Tries the literal symbol first (so ``SKP-SM`` hits its NSE SME row), then
    a cleaned variant for Zerodha series-suffixed symbols, then ``{sym}-SM``
    for journal entries that dropped the SME suffix. Preference within each
    candidate: NSE → BSE → any exchange.

    Numeric journal symbols are treated as BSE scrip codes and matched on
    ``exchange_token`` (which is where BSE scrip codes live).
    """
    from .models import KiteInstrument

    candidates = _symbol_candidates(symbol)

    for candidate in candidates:
        q = db.query(KiteInstrument).filter(KiteInstrument.tradingsymbol == candidate)
        for exch in ("NSE", "BSE"):
            row = q.filter(KiteInstrument.exchange == exch).first()
            if row:
                return row
        row = q.first()
        if row:
            return row

    # Numeric fall-through: match BSE scrip code on exchange_token.
    literal = (symbol or "").strip().upper()
    if literal.isdigit():
        return (
            db.query(KiteInstrument)
            .filter(
                KiteInstrument.exchange == "BSE",
                KiteInstrument.exchange_token == int(literal),
            )
            .first()
        )
    return None


# -- Instrument master -------------------------------------------------------


def sync_instruments(db: Session, exchanges: tuple[str, ...] = ("NSE", "BSE")) -> dict:
    """Pull the full instrument dump for the given exchanges, replace the cache."""
    from .models import KiteInstrument

    kc = client(db)
    if kc is None:
        raise RuntimeError("Not authenticated with Kite")

    total = 0
    db.query(KiteInstrument).filter(KiteInstrument.exchange.in_(exchanges)).delete(
        synchronize_session=False
    )
    db.flush()

    for exch in exchanges:
        rows = kc.instruments(exch)
        for r in rows:
            # Focus on equity rows; skip derivatives by default.
            if r.get("segment") not in (f"{exch}", "BSE", "NSE"):
                pass  # Kite returns the segment; we keep all equity rows
            db.add(
                KiteInstrument(
                    instrument_token=int(r["instrument_token"]),
                    exchange_token=int(r.get("exchange_token") or 0),
                    tradingsymbol=str(r["tradingsymbol"]).upper(),
                    name=(r.get("name") or "").strip() or None,
                    exchange=exch,
                    segment=str(r.get("segment") or "")[:50],
                    instrument_type=str(r.get("instrument_type") or "")[:20],
                    lot_size=int(r.get("lot_size") or 0),
                    tick_size=float(r.get("tick_size") or 0),
                )
            )
            total += 1
        db.flush()

    db.commit()
    app_settings.set_value(db, "kite_instruments_synced_at", datetime.utcnow().isoformat())
    db.commit()
    return {"total": total, "exchanges": list(exchanges)}
