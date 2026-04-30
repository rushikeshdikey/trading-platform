"""Audited Kite Connect wrapper.

Every Kite call we make in the trading engine MUST go through this module.
It captures (endpoint, request kwargs, response, latency, status, error)
into ``broker_audit`` so we can reconstruct exactly what happened weeks
later when something looks wrong.

Pattern:

    with kite_audited.session(db, user) as kc:
        holdings = kc.call("holdings", kc.client.holdings)

`kc.call(name, fn, *args, **kwargs)` is the only way to invoke a Kite
SDK method. It returns the SDK's return value (or re-raises). The audit
row is committed even on exception — debug visibility is more important
than transactional purity here.

Why a thin wrapper instead of monkey-patching kiteconnect: a wrapper makes
the audit boundary obvious in code review, and we control which methods
are reachable. The trading engine should never call ``kc.client.foo()``
directly — only ``kc.call("foo", kc.client.foo, ...)``.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator

from sqlalchemy.orm import Session

from .. import kite as kite_mod
from ..models import BrokerAudit, User

log = logging.getLogger("journal.trading_engine.kite")


def _safe_json(value: Any, max_chars: int = 16_000) -> str | None:
    """Best-effort JSON serialise. Truncates very large blobs.

    Kite's ``instruments()`` returns ~100k rows. We never log it through
    here (sync_instruments is treated as a maintenance op), but cap anyway
    so an accidental large response doesn't bloat the audit table.
    """
    if value is None:
        return None
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        s = f"<unserialisable {type(value).__name__}: {exc}>"
    if len(s) > max_chars:
        return s[:max_chars] + f"...<truncated {len(s) - max_chars} chars>"
    return s


class AuditedKite:
    """Per-request handle. Bind once with (db, user); call methods via
    ``call(endpoint_name, fn, ...)``. Always check ``is_authed`` first."""

    def __init__(self, db: Session, user: User):
        self.db = db
        self.user = user
        self.client = kite_mod.client(user)  # None if not authed

    @property
    def is_authed(self) -> bool:
        return self.client is not None

    def call(self, endpoint: str, fn: Callable, *args, **kwargs) -> Any:
        """Invoke a Kite SDK method with full audit capture.

        Always writes a BrokerAudit row — on success and on failure. The
        SDK's return value is returned to the caller; SDK exceptions are
        re-raised after logging.
        """
        if self.client is None:
            self._record(endpoint, kwargs, None, status=0,
                         latency_ms=0, error="not_authed")
            raise RuntimeError("kite client not authenticated for this user")

        request_payload = self._capture_request(args, kwargs)
        t0 = time.perf_counter()
        try:
            response = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.perf_counter() - t0) * 1000)
            self._record(
                endpoint, request_payload, None,
                status=getattr(exc, "code", 0) or 0,
                latency_ms=latency_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        latency_ms = int((time.perf_counter() - t0) * 1000)
        self._record(
            endpoint, request_payload, response,
            status=200, latency_ms=latency_ms, error=None,
        )
        return response

    def _capture_request(self, args: tuple, kwargs: dict) -> dict:
        """Capture call inputs as a single dict for the audit row.

        Positional args become "args[i]" keys. Avoids JSON blowup if the
        caller passes large lists (instruments(), etc.).
        """
        payload: dict = dict(kwargs)
        if args:
            payload["__args__"] = list(args)
        return payload

    def _record(
        self, endpoint: str, request_payload: Any, response: Any,
        *, status: int, latency_ms: int, error: str | None,
    ) -> None:
        """Insert the audit row and commit immediately.

        Standalone commit (not piggybacking on the caller's transaction)
        so an exception in the caller doesn't roll back the audit. We
        explicitly want the failure trail.
        """
        try:
            self.db.add(BrokerAudit(
                user_id=self.user.id,
                created_at=datetime.utcnow(),
                endpoint=endpoint,
                request_json=_safe_json(request_payload),
                response_json=_safe_json(response),
                status=status,
                latency_ms=latency_ms,
                error=error,
            ))
            self.db.commit()
        except Exception:  # noqa: BLE001
            log.exception("broker_audit write failed for endpoint=%s", endpoint)
            try:
                self.db.rollback()
            except Exception:  # noqa: BLE001
                pass


@contextmanager
def session(db: Session, user: User) -> Iterator[AuditedKite]:
    """Context manager around an AuditedKite. Currently a thin shim — exists
    so future versions can hold connection pooling, rate-limit slot, etc.
    without changing call sites.
    """
    handle = AuditedKite(db, user)
    try:
        yield handle
    finally:
        # Nothing to release for now. KiteConnect SDK uses requests'
        # global pool; no per-session resource.
        pass


# ---------------------------------------------------------------------------
# Read-only convenience methods. Phase E0 surface.
# ---------------------------------------------------------------------------


def fetch_holdings(db: Session, user: User) -> list[dict]:
    """Phase E0: list of held instruments with avg_price + last_price + qty.
    Read-only; safe to call any time. Empty list if not authed."""
    with session(db, user) as kc:
        if not kc.is_authed:
            return []
        return kc.call("holdings", kc.client.holdings) or []


def fetch_positions(db: Session, user: User) -> dict:
    """Phase E0: net + day positions. The TSL daemon's data source — each
    position dict has 'last_price', updated server-side."""
    with session(db, user) as kc:
        if not kc.is_authed:
            return {"net": [], "day": []}
        out = kc.call("positions", kc.client.positions) or {}
        return {"net": out.get("net", []), "day": out.get("day", [])}


def fetch_profile(db: Session, user: User) -> dict:
    """Phase E0: liveness probe. Returns Kite user_id + name. Used by the
    /admin/kite page to confirm the session is alive."""
    with session(db, user) as kc:
        if not kc.is_authed:
            return {}
        return kc.call("profile", kc.client.profile) or {}


def fetch_margins(db: Session, user: User) -> dict:
    """Phase E0: equity margins (cash, used, available)."""
    with session(db, user) as kc:
        if not kc.is_authed:
            return {}
        return kc.call("margins_equity", kc.client.margins, "equity") or {}


def fetch_gtts(db: Session, user: User) -> list[dict]:
    """Phase E0: existing GTT triggers. Useful for reconciliation —
    every open Trade should typically have an associated GTT once we're
    in Phase E1+."""
    with session(db, user) as kc:
        if not kc.is_authed:
            return []
        return kc.call("get_gtts", kc.client.get_gtts) or []


def fetch_orders(db: Session, user: User) -> list[dict]:
    """Phase E1.1: today's order list (open + completed + rejected).

    Used by the pending-entry resolver to find the BUY order spawned by a
    GTT-single trigger and check whether it filled. Each row has fields
    like order_id, status, average_price, filled_quantity, transaction_type.
    Empty list if not authed.
    """
    with session(db, user) as kc:
        if not kc.is_authed:
            return []
        return kc.call("get_orders", kc.client.orders) or []


# ---------------------------------------------------------------------------
# Phase E1 write surface — order placement.
#
# Every method here writes to the broker. They share three preconditions:
#   1. Authed Kite session (raises if not)
#   2. Resolved KiteInstrument (raises if symbol can't be mapped to a real
#      tradingsymbol + exchange)
#   3. Sanity-checked sizes (qty > 0, stop < entry < target for long, etc.)
# ---------------------------------------------------------------------------


def place_gtt_oco(
    db: Session, user: User, *,
    symbol: str, qty: int,
    entry_price: float, stop_price: float, target_price: float,
    transaction_type: str = "BUY",
) -> dict:
    """Place a GTT-OCO (One-Cancels-Other) bracket on Kite.

    OCO has three legs: entry trigger + SL trigger + target trigger.
    When entry fires, both SL + target legs activate; whichever hits
    first cancels the other.

    For BUY trades: stop_price < entry_price < target_price.

    Returns Kite's response dict; the broker's GTT id is in
    response["trigger_id"]. Save that on the Trade row so we can later
    modify or cancel.
    """
    from .. import kite as kite_mod

    if transaction_type not in ("BUY", "SELL"):
        raise ValueError(f"transaction_type must be BUY or SELL, got {transaction_type}")
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")
    if transaction_type == "BUY":
        if not (stop_price < entry_price < target_price):
            raise ValueError(
                f"BUY OCO requires stop < entry < target; got "
                f"stop={stop_price} entry={entry_price} target={target_price}"
            )
    else:  # SELL
        if not (target_price < entry_price < stop_price):
            raise ValueError(
                f"SELL OCO requires target < entry < stop; got "
                f"stop={stop_price} entry={entry_price} target={target_price}"
            )

    inst = kite_mod._resolve_instrument(db, symbol)
    if inst is None:
        raise RuntimeError(f"can't resolve {symbol} to a Kite instrument")

    # Kite GTT API parameters. Trigger values are always sorted ascending.
    trigger_values = sorted([stop_price, target_price])
    last_price = entry_price  # used by Kite for sanity, not the actual trigger

    with session(db, user) as kc:
        if not kc.is_authed:
            raise RuntimeError("not authed with Kite")

        # Order legs that fire when each trigger hits. OCO = exactly 2 orders
        # of opposite intent for a BUY entry: when entry fires, BOTH legs
        # become active; first to trigger cancels the other.
        # For BUY: leg1 = SL-sell at stop, leg2 = target-sell at target
        leg_transaction = "SELL" if transaction_type == "BUY" else "BUY"
        orders = [
            {
                "exchange": inst.exchange,
                "tradingsymbol": inst.tradingsymbol,
                "transaction_type": leg_transaction,
                "quantity": qty,
                "order_type": "LIMIT",
                "product": "CNC",  # delivery — swing trade
                "price": trigger_values[0],
            },
            {
                "exchange": inst.exchange,
                "tradingsymbol": inst.tradingsymbol,
                "transaction_type": leg_transaction,
                "quantity": qty,
                "order_type": "LIMIT",
                "product": "CNC",
                "price": trigger_values[1],
            },
        ]

        return kc.call(
            "place_gtt",
            kc.client.place_gtt,
            trigger_type=kc.client.GTT_TYPE_OCO,
            tradingsymbol=inst.tradingsymbol,
            exchange=inst.exchange,
            trigger_values=trigger_values,
            last_price=last_price,
            orders=orders,
        )


def cancel_gtt(db: Session, user: User, trigger_id: int) -> dict:
    """Cancel a GTT trigger by its Kite-side ID."""
    with session(db, user) as kc:
        if not kc.is_authed:
            raise RuntimeError("not authed with Kite")
        return kc.call("delete_gtt", kc.client.delete_gtt, trigger_id=trigger_id)


def _reference_last_price(db: Session, symbol: str, fallback: float) -> float:
    """Best-effort 'current' last price for Kite's GTT sanity check.

    Kite's place_gtt requires last_price ≠ trigger_price (their server
    rejects "Trigger cannot be created with trigger price equal to the
    last price"). We can't call ``kc.ltp()`` because the user's app lacks
    the paid Quote subscription, so we reach for the most recent close in
    our ``daily_bars`` cache. If even that's missing, fall back to the
    given ``fallback`` ± 0.10 to guarantee inequality.
    """
    from ..models import DailyBar
    bar = (
        db.query(DailyBar)
        .filter(DailyBar.symbol == symbol.upper())
        .order_by(DailyBar.date.desc())
        .first()
    )
    if bar is not None and bar.close and abs(bar.close - fallback) >= 0.05:
        return float(bar.close)
    # Worst case: nudge the fallback so it's unequal — direction doesn't
    # matter for Kite's sanity check; only inequality does.
    return float(fallback) - 0.10 if fallback > 0.10 else float(fallback) + 0.10


def place_gtt_single_buy(
    db: Session, user: User, *,
    symbol: str, qty: int, trigger_price: float,
) -> dict:
    """Place a GTT-single BUY trigger — fires a LIMIT BUY when LTP touches
    ``trigger_price``. Used by the hybrid entry mode 'trigger' path; the
    OCO bracket is placed later, after the BUY fills.

    Returns Kite's response dict; the broker's GTT id is in ``trigger_id``.
    """
    from .. import kite as kite_mod

    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")
    if trigger_price <= 0:
        raise ValueError(f"trigger_price must be positive, got {trigger_price}")

    inst = kite_mod._resolve_instrument(db, symbol)
    if inst is None:
        raise RuntimeError(f"can't resolve {symbol} to a Kite instrument")

    orders = [{
        "exchange": inst.exchange,
        "tradingsymbol": inst.tradingsymbol,
        "transaction_type": "BUY",
        "quantity": qty,
        "order_type": "LIMIT",
        "product": "CNC",
        "price": trigger_price,
    }]

    last_price = _reference_last_price(db, symbol, fallback=trigger_price)

    with session(db, user) as kc:
        if not kc.is_authed:
            raise RuntimeError("not authed with Kite")
        return kc.call(
            "place_gtt",
            kc.client.place_gtt,
            trigger_type=kc.client.GTT_TYPE_SINGLE,
            tradingsymbol=inst.tradingsymbol,
            exchange=inst.exchange,
            trigger_values=[trigger_price],
            last_price=last_price,
            orders=orders,
        )


def place_order_market(
    db: Session, user: User, *,
    symbol: str, qty: int, transaction_type: str,
    market_protection_pct: float = 1.0,
) -> dict:
    """Place a regular MARKET order (CNC, REGULAR variety).

    Kite Connect rejects naked MARKET orders via API ("Market orders
    without market protection are not allowed"). The fix is the
    ``market_protection`` parameter — a percentage slippage band that
    converts the market order into a LIMIT-with-bands internally. 1%
    is wide enough to fill even on volatile days for liquid Indian
    cash-equity names.

    Used by:
      - "Buy now" entry mode in /trading/gtt/submit (immediate BUY)
      - "Exit at market" button on /positions (immediate SELL)

    Returns Kite's response dict; the broker's order id is in
    ``response["order_id"]``.
    """
    from .. import kite as kite_mod

    if transaction_type not in ("BUY", "SELL"):
        raise ValueError(f"transaction_type must be BUY or SELL, got {transaction_type}")
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")

    inst = kite_mod._resolve_instrument(db, symbol)
    if inst is None:
        raise RuntimeError(f"can't resolve {symbol} to a Kite instrument")

    with session(db, user) as kc:
        if not kc.is_authed:
            raise RuntimeError("not authed with Kite")
        return kc.call(
            "place_order",
            kc.client.place_order,
            variety=kc.client.VARIETY_REGULAR,
            exchange=inst.exchange,
            tradingsymbol=inst.tradingsymbol,
            transaction_type=transaction_type,
            quantity=qty,
            order_type=kc.client.ORDER_TYPE_MARKET,
            product=kc.client.PRODUCT_CNC,
            market_protection=market_protection_pct,
        )


def modify_gtt(
    db: Session, user: User, trigger_id: int, *,
    symbol: str, qty: int,
    stop_price: float, target_price: float,
    transaction_type: str = "BUY",
    last_price: float | None = None,
) -> dict:
    """Modify an existing GTT-OCO — used by the TSL daemon to ratchet SL up."""
    from .. import kite as kite_mod

    inst = kite_mod._resolve_instrument(db, symbol)
    if inst is None:
        raise RuntimeError(f"can't resolve {symbol} to a Kite instrument")

    trigger_values = sorted([stop_price, target_price])
    leg_transaction = "SELL" if transaction_type == "BUY" else "BUY"
    orders = [
        {
            "exchange": inst.exchange, "tradingsymbol": inst.tradingsymbol,
            "transaction_type": leg_transaction, "quantity": qty,
            "order_type": "LIMIT", "product": "CNC", "price": trigger_values[0],
        },
        {
            "exchange": inst.exchange, "tradingsymbol": inst.tradingsymbol,
            "transaction_type": leg_transaction, "quantity": qty,
            "order_type": "LIMIT", "product": "CNC", "price": trigger_values[1],
        },
    ]

    with session(db, user) as kc:
        if not kc.is_authed:
            raise RuntimeError("not authed with Kite")
        return kc.call(
            "modify_gtt",
            kc.client.modify_gtt,
            trigger_id=trigger_id,
            trigger_type=kc.client.GTT_TYPE_OCO,
            tradingsymbol=inst.tradingsymbol,
            exchange=inst.exchange,
            trigger_values=trigger_values,
            last_price=last_price or trigger_values[0],
            orders=orders,
        )
