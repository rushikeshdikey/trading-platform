"""Instrument search endpoint — powers the autocomplete on `+ New trade`.

Prefers the Kite instrument master when it's been synced (has nice names and
covers every NSE/BSE/SME scrip). Falls back to the user's own past trade
symbols when Kite hasn't been connected yet, so the feature still works for
new users.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import KiteInstrument, Trade

router = APIRouter(prefix="/instruments")


@router.get("/search")
def search(q: str = "", limit: int = 20, db: Session = Depends(get_db)):
    q = (q or "").strip().upper()
    if not q:
        return []
    limit = max(1, min(limit, 50))

    results: list[dict] = []
    seen: set[str] = set()

    # 1) Kite instrument master (primary source once user has logged in).
    kite_rows = (
        db.query(KiteInstrument)
        .filter(KiteInstrument.tradingsymbol.like(f"{q}%"))
        .filter(
            KiteInstrument.instrument_type.in_(("EQ", "SM"))
            | KiteInstrument.instrument_type.is_(None)
        )
        .order_by(
            # Exact match first, then by exchange (NSE before BSE), then alpha
            (KiteInstrument.tradingsymbol == q).desc(),
            KiteInstrument.exchange.asc(),
            KiteInstrument.tradingsymbol.asc(),
        )
        .limit(limit)
        .all()
    )
    for row in kite_rows:
        if row.tradingsymbol in seen:
            continue
        seen.add(row.tradingsymbol)
        results.append(
            {
                "symbol": row.tradingsymbol,
                "name": row.name or "",
                "exchange": row.exchange,
            }
        )

    # 2) Fallback / supplement — user's own trade history.
    if len(results) < limit:
        remaining = limit - len(results)
        rows = (
            db.query(Trade.instrument, func.count(Trade.id).label("n"))
            .filter(Trade.instrument.like(f"{q}%"))
            .group_by(Trade.instrument)
            .order_by(func.count(Trade.id).desc())
            .limit(remaining)
            .all()
        )
        for sym, _ in rows:
            if sym in seen:
                continue
            seen.add(sym)
            results.append({"symbol": sym, "name": "", "exchange": ""})

    return results
