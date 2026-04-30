"""Nifty 50 index fetcher + distribution-day counter.

Fetches ^NSEI from yfinance (the same path breadth.py uses for stocks)
and stores daily OHLCV in nifty_daily. Distribution day count is
computed off this table.

DISTRIBUTION DAY (O'Neil):
  Index closes -0.2% or worse on volume HIGHER than the prior session
  = institutional selling pressure.

  >= 5 distribution days in 25 sessions: market is topping/correcting.
  When the count drops to 0, market has "reset" and uptrend can resume.

Used by:
  - /breadth panel (display the count + last-25 timeline)
  - regime multiplier (future enhancement, not v1)

Fetched in the EOD prewarm. Cheap — one yfinance call per day.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from ..models import NiftyDaily

log = logging.getLogger("journal.scanner.nifty_index")


# Threshold definitions (configurable; conservative defaults).
DIST_DAY_THRESHOLD_PCT = -0.002    # -0.2%
DIST_LOOKBACK_SESSIONS = 25         # rolling window
DIST_TOPPING_THRESHOLD = 5          # >=5 in lookback = topping signal


def fetch_and_store(db: Session, lookback_days: int = 60) -> dict:
    """Pull ^NSEI from yfinance, upsert into nifty_daily.

    Returns a summary dict; never raises (yfinance failures are common
    and shouldn't break the prewarm).
    """
    summary = {"fetched": 0, "upserted": 0, "skipped_existing": 0, "error": None}
    try:
        import yfinance as yf
        end = date.today() + timedelta(days=1)
        start = end - timedelta(days=lookback_days)
        df = yf.download(
            "^NSEI", start=start, end=end,
            progress=False, auto_adjust=False, threads=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("nifty_index fetch failed: %s", exc)
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary

    if df is None or df.empty:
        summary["error"] = "yfinance returned empty frame for ^NSEI"
        return summary

    # yfinance's MultiIndex columns when threads=False on a single ticker
    # collapse to a single level — but defensively flatten if present.
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)

    existing_dates = {
        d for (d,) in db.query(NiftyDaily.date)
        .filter(NiftyDaily.date >= start)
        .all()
    }

    for ts, row in df.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        summary["fetched"] += 1
        if d in existing_dates:
            summary["skipped_existing"] += 1
            continue
        try:
            db.add(NiftyDaily(
                date=d,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=int(row.get("Volume", 0) or 0),
                fetched_at=datetime.utcnow(),
            ))
            summary["upserted"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("nifty_index row %s skip: %s", d, exc)
            continue

    if summary["upserted"]:
        db.commit()
    return summary


def latest_n(db: Session, n: int = 25) -> list[NiftyDaily]:
    """Last ``n`` Nifty bars, most-recent first."""
    return (
        db.query(NiftyDaily)
        .order_by(NiftyDaily.date.desc())
        .limit(n)
        .all()
    )


def distribution_day_flags(bars: list[NiftyDaily]) -> list[tuple[date, bool, float, int]]:
    """For each bar (oldest first), return (date, is_distribution, pct, volume).

    A bar is a distribution day iff:
      - close pct change <= DIST_DAY_THRESHOLD_PCT (-0.2%)
      - volume > prior session's volume

    Bar #0 has no prior, so always non-distribution. Caller should hand
    in chronologically-ascending bars.
    """
    out: list[tuple[date, bool, float, int]] = []
    prev = None
    for b in bars:
        if prev is None:
            out.append((b.date, False, 0.0, b.volume))
            prev = b
            continue
        pct = (b.close - prev.close) / prev.close if prev.close else 0.0
        is_dist = pct <= DIST_DAY_THRESHOLD_PCT and b.volume > prev.volume
        out.append((b.date, is_dist, pct, b.volume))
        prev = b
    return out


def count_distribution_days(db: Session, lookback: int = DIST_LOOKBACK_SESSIONS) -> dict:
    """Compute current distribution-day count over the last ``lookback`` sessions.

    Returns:
      {
        count: int,                 # distribution days in window
        topping: bool,              # count >= DIST_TOPPING_THRESHOLD
        bars: int,                  # bars actually examined (≤ lookback)
        latest_close: float | None,
        latest_date: date | None,
        timeline: [{date, is_dist, pct, vol}],   # most-recent last
      }
    """
    bars_desc = latest_n(db, lookback)
    bars = list(reversed(bars_desc))   # ascending for the flag function

    flags = distribution_day_flags(bars)
    count = sum(1 for (_, is_d, _, _) in flags if is_d)
    latest = bars[-1] if bars else None
    return {
        "count": count,
        "topping": count >= DIST_TOPPING_THRESHOLD,
        "topping_threshold": DIST_TOPPING_THRESHOLD,
        "lookback_sessions": lookback,
        "bars": len(bars),
        "latest_close": latest.close if latest else None,
        "latest_date": latest.date if latest else None,
        "timeline": [
            {"date": d.isoformat(), "is_dist": is_d,
             "pct": round(pct * 100, 2), "vol": vol}
            for (d, is_d, pct, vol) in flags
        ],
    }
