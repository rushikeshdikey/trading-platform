"""Zerodha Kite Connect integration — per-user.

Each user has their own Kite developer-app credentials (api_key + api_secret)
stored as Fernet-encrypted blobs on the User row. The daily access_token also
lives there. Kite's access_token rolls at ~06:00 IST; we store
`kite_token_expires_at` and treat anything past it as logged out.

Public API takes a `User` parameter throughout. The env-var creds in
`app/config.py` are a fallback ONLY when a user hasn't supplied their own
keys yet — useful for the bootstrap admin during initial setup.

Instrument-master sync writes to a SHARED table (`kite_instruments`) so
every user benefits from one user's sync; any authed user can refresh it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from . import auth as auth_mod
from . import config
from .models import User

log = logging.getLogger("journal.kite")

IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    """Kite rolls tokens at ~06:00 IST — anything before that is "still yesterday"."""
    now = datetime.now(tz=IST)
    if now.time() < dtime(6, 0):
        return (now - timedelta(days=1)).date()
    return now.date()


# -- Per-user credential resolution -----------------------------------------


def _api_key(user: User) -> str | None:
    """User-stored key; falls back to env (bootstrap admin convenience)."""
    if user.kite_api_key_enc:
        try:
            return auth_mod.decrypt_str(user.kite_api_key_enc)
        except Exception:  # noqa: BLE001
            log.exception("failed to decrypt kite_api_key for user_id=%s", user.id)
    return config.settings.kite_api_key


def _api_secret(user: User) -> str | None:
    if user.kite_api_secret_enc:
        try:
            return auth_mod.decrypt_str(user.kite_api_secret_enc)
        except Exception:  # noqa: BLE001
            log.exception("failed to decrypt kite_api_secret for user_id=%s", user.id)
    return config.settings.kite_api_secret


def _access_token(user: User) -> str | None:
    if user.kite_access_token_enc:
        try:
            return auth_mod.decrypt_str(user.kite_access_token_enc)
        except Exception:  # noqa: BLE001
            return None
    return None


def is_configured(user: User) -> bool:
    return bool(_api_key(user) and _api_secret(user))


def is_authed(user: User) -> bool:
    if not _access_token(user):
        return False
    if user.kite_token_expires_at is None:
        return False
    # Stored as a UTC-naive datetime; convert _today_ist to a naive comparison.
    return user.kite_token_expires_at >= datetime.utcnow()


def auth_status(user: User) -> dict[str, Any]:
    return {
        "configured": is_configured(user),
        "authed": is_authed(user),
        "user_id": None,  # Kite user id — not exposed in this minimal status
        "user_name": None,
        "token_expires_at": (
            user.kite_token_expires_at.isoformat()
            if user.kite_token_expires_at else None
        ),
    }


# -- OAuth-ish flow ---------------------------------------------------------


def login_url(user: User) -> str:
    """URL the user is sent to for the Kite login flow."""
    api_key = _api_key(user)
    if not api_key:
        raise RuntimeError(
            "Kite is not configured for this account. Add API key + secret in /account."
        )
    from kiteconnect import KiteConnect
    return KiteConnect(api_key=api_key).login_url()


def exchange_request_token(db: Session, user: User, request_token: str) -> dict[str, Any]:
    """Swap a ``request_token`` for an ``access_token`` and persist it on the
    user. Returns Kite's ``generate_session`` response so the caller can show
    the user's Kite name/id if it wants."""
    api_key = _api_key(user)
    api_secret = _api_secret(user)
    if not (api_key and api_secret):
        raise RuntimeError(
            "Kite credentials missing — add them in /account before logging in."
        )
    from kiteconnect import KiteConnect

    kc = KiteConnect(api_key=api_key)
    data = kc.generate_session(request_token, api_secret=api_secret)
    user.kite_access_token_enc = auth_mod.encrypt_str(data["access_token"])
    # Treat the token as valid until 06:00 IST tomorrow — Kite's rotation cadence.
    tomorrow_6am_ist = datetime.now(tz=IST).replace(
        hour=6, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    # Store as naive UTC for SQLAlchemy DateTime column consistency.
    user.kite_token_expires_at = tomorrow_6am_ist.astimezone(timezone.utc).replace(tzinfo=None)
    db.commit()
    return data


def logout(db: Session, user: User) -> None:
    user.kite_access_token_enc = None
    user.kite_token_expires_at = None
    db.commit()


def save_credentials(
    db: Session, user: User, api_key: str | None, api_secret: str | None,
) -> None:
    """Encrypt and store the user's Kite developer-app credentials. Empty
    strings clear them (so the user can revoke)."""
    if api_key:
        user.kite_api_key_enc = auth_mod.encrypt_str(api_key.strip())
    elif api_key == "":
        user.kite_api_key_enc = None
    if api_secret:
        user.kite_api_secret_enc = auth_mod.encrypt_str(api_secret.strip())
    elif api_secret == "":
        user.kite_api_secret_enc = None
    # Re-saving credentials invalidates any access_token (creds may have changed).
    user.kite_access_token_enc = None
    user.kite_token_expires_at = None
    db.commit()


# -- Authenticated client ---------------------------------------------------


def client(user: User):
    """Return an authenticated KiteConnect client, or None if not authed."""
    if not is_authed(user):
        return None
    api_key = _api_key(user)
    token = _access_token(user)
    if not (api_key and token):
        return None
    from kiteconnect import KiteConnect
    kc = KiteConnect(api_key=api_key)
    kc.set_access_token(token)
    return kc


# -- LTP --------------------------------------------------------------------


def ltp(db: Session, user: User, symbols: list[str]) -> dict[str, float]:
    """Fetch last traded prices for the given journal symbols using ``user``'s
    Kite session. Empty dict if not authed or symbols can't be resolved."""
    from .models import KiteInstrument  # noqa: F401  -- import for type narrowing

    kc = client(user)
    if kc is None or not symbols:
        return {}

    out: dict[str, float] = {}
    kite_keys: dict[str, str] = {}
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
    literal = (symbol or "").strip().upper()
    out: list[str] = [literal]
    cleaned = _clean_symbol(literal)
    if cleaned and cleaned not in out:
        out.append(cleaned)
    if literal and "-" not in literal and not literal.isdigit():
        out.append(f"{literal}-SM")
    return out


def _resolve_instrument(db: Session, symbol: str):
    """Best-effort lookup in the SHARED ``kite_instruments`` table. NSE → BSE
    preference. Numeric symbols match BSE scrip codes by ``exchange_token``."""
    from .models import KiteInstrument

    # KiteInstrument is shared (no user_id) — but the auto-filter only acts on
    # models that have user_id, so a vanilla query is fine.
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


# -- Instrument master ------------------------------------------------------


def sync_instruments(
    db: Session, user: User, exchanges: tuple[str, ...] = ("NSE", "BSE"),
) -> dict:
    """Pull the full instrument dump for the given exchanges, replace the cache.

    Uses ``user``'s Kite session, but writes to the SHARED
    ``kite_instruments`` table — every user benefits from one user's sync.
    """
    from .models import KiteInstrument

    kc = client(user)
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
    return {"total": total, "exchanges": list(exchanges)}
