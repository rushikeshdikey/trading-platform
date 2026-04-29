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
