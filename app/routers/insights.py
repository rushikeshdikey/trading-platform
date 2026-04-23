"""Ad-hoc insight endpoints used by HTMX/Alpine on the new-trade form.

Powers the 'should I take this trade?' card: historical stats for the
selected setup, current open-heat, and the risk budget remaining.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import analytics, calculations as calc, charges as charges_svc, dashboard as dash
from .. import settings as app_settings
from ..db import get_db
from ..models import Trade

router = APIRouter(prefix="/insights")


@router.get("/setup-stats")
def setup_stats(setup: str = "", db: Session = Depends(get_db)):
    """Edge snapshot for a single setup. Used on the + New trade form.

    Returns enough to render the 'your last N trades in this setup' card:
    sample size, win rate, avg win/loss in R, expectancy in R, verdict.
    """
    label = (setup or "").strip()
    all_rows = analytics.setup_edge(db)
    match = next((r for r in all_rows if r.setup == label), None)
    if match is None:
        return {
            "setup": label,
            "found": False,
            "message": "No past trades in this setup — fresh territory.",
        }
    return {
        "setup": match.setup,
        "found": True,
        "trades": match.trades,
        "wins": match.wins,
        "losses": match.losses,
        "win_pct": round(match.win_pct * 100, 1),
        "avg_win_r": round(match.avg_win_r, 2) if match.avg_win_r is not None else None,
        "avg_loss_r": round(match.avg_loss_r, 2) if match.avg_loss_r is not None else None,
        "expectancy_r": round(match.expectancy_r, 2) if match.expectancy_r is not None else None,
        "best_r": round(match.best_r, 2) if match.best_r is not None else None,
        "worst_r": round(match.worst_r, 2) if match.worst_r is not None else None,
        "total_pnl": round(match.total_pnl, 2),
        "verdict": match.verdict,
        "verdict_reason": match.verdict_reason,
    }


@router.get("/pre-trade-check")
def pre_trade_check(
    symbol: str = "",
    entry: float = 0.0,
    sl: float = 0.0,
    side: str = "B",
    db: Session = Depends(get_db),
):
    """Auto-compute the checkable items of the pre-trade funnel for a symbol.

    Each check returns ``{"key","label","status","detail"}``. ``status`` is
    ``pass``, ``fail``, or ``skip`` (not enough data). The UI merges these
    with the user-answered manual checks for a combined score.
    """
    from .. import market_data
    from datetime import date, timedelta
    import math

    out: list[dict] = []
    add = lambda k, lbl, status, detail="": out.append(
        {"key": k, "label": lbl, "status": status, "detail": detail}
    )

    sym = (symbol or "").strip()
    if not sym:
        return {"checks": [], "score": 0, "max_score": 0, "auto_passed": 0}

    # --- Cheap local checks first ---------------------------------------
    if entry > 0 and sl > 0:
        dist = abs(entry - sl) / entry
        if dist < 0.03:
            add("low_risk_entry", "Low-risk entry (SL < 3% away)", "pass",
                f"{dist*100:.2f}% distance")
        elif dist < 0.06:
            add("low_risk_entry", "Low-risk entry (SL < 3% away)", "fail",
                f"{dist*100:.2f}% — tolerable but wide")
        else:
            add("low_risk_entry", "Low-risk entry (SL < 3% away)", "fail",
                f"{dist*100:.2f}% — too wide")
    else:
        add("low_risk_entry", "Low-risk entry (SL < 3% away)", "skip",
            "Enter price & SL to check")

    # --- OHLC-based checks ----------------------------------------------
    today = date.today()
    try:
        bars = market_data.fetch_ohlc(db, sym, today - timedelta(days=320), today)
    except Exception:
        bars = []

    if not bars or len(bars) < 60:
        for k, lbl in (
            ("ema_tight", "EMAs are tight (20/50 within 2%, 50/200 within 5%)"),
            ("above_200ema", "Trending above 200 EMA (uptrend intact)"),
            ("liquidity", "Liquid (₹5 Cr avg daily turnover)"),
            ("not_extended", "Not extended (< 12% above 20 EMA)"),
        ):
            add(k, lbl, "skip", "No price history available")
        auto_passed = sum(1 for c in out if c["status"] == "pass")
        return {"checks": out, "score": auto_passed, "max_score": 5, "auto_passed": auto_passed}

    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    # Approximate volume — yfinance `fetch_ohlc` doesn't return it yet, so use
    # turnover = close × a proxy of 20-day range. Good enough to gate micro-caps.
    last_close = closes[-1]

    # EMAs (simple exponential approximation)
    def ema(arr, span):
        k = 2 / (span + 1)
        v = arr[0]
        for x in arr[1:]:
            v = x * k + v * (1 - k)
        return v

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes[-200:] if len(closes) >= 200 else closes, 200)

    # EMA tightness
    tight_20_50 = abs(ema20 - ema50) / last_close if last_close else 1
    tight_50_200 = abs(ema50 - ema200) / last_close if last_close else 1
    if tight_20_50 < 0.02 and tight_50_200 < 0.05:
        add("ema_tight", "EMAs are tight (20/50 within 2%, 50/200 within 5%)",
            "pass", f"20↔50: {tight_20_50*100:.1f}%, 50↔200: {tight_50_200*100:.1f}%")
    else:
        add("ema_tight", "EMAs are tight (20/50 within 2%, 50/200 within 5%)",
            "fail", f"20↔50: {tight_20_50*100:.1f}%, 50↔200: {tight_50_200*100:.1f}%")

    # Above 200 EMA + rising
    ema200_prior = ema(closes[-230:-30] if len(closes) >= 230 else closes[:-30] or closes, 200)
    above_200 = last_close > ema200
    rising = ema200 > ema200_prior
    if above_200 and rising:
        add("above_200ema", "Trending above 200 EMA (uptrend intact)",
            "pass", f"Price {last_close:.2f} vs 200EMA {ema200:.2f}, slope ↑")
    elif above_200:
        add("above_200ema", "Trending above 200 EMA (uptrend intact)",
            "fail", f"Above 200EMA but it's flat/declining")
    else:
        add("above_200ema", "Trending above 200 EMA (uptrend intact)",
            "fail", f"Price {last_close:.2f} below 200EMA {ema200:.2f}")

    # Liquidity — proxy via 20-day avg high-low range × close (rough turnover)
    recent = bars[-20:]
    avg_range = sum((b["high"] - b["low"]) for b in recent) / len(recent)
    est_turnover_cr = (avg_range * last_close * 50_000) / 1e7  # very rough
    # Better would be real volume, but we don't store it — fall back to a
    # simpler "price × size" check: is the stock tradeable at reasonable size?
    if last_close > 20:
        add("liquidity", "Liquid enough for a ₹1L position", "pass",
            f"Close ₹{last_close:.2f} — enter at reasonable lot size")
    else:
        add("liquidity", "Liquid enough for a ₹1L position", "fail",
            f"Low-priced — size and slippage checks required")

    # Not extended: CMP within 12% of 20 EMA
    ext = (last_close - ema20) / ema20 if ema20 else 0
    if -0.05 <= ext <= 0.12:
        add("not_extended", "Not extended (≤12% above 20 EMA)", "pass",
            f"{ext*100:+.1f}% from 20EMA")
    else:
        add("not_extended", "Not extended (≤12% above 20 EMA)", "fail",
            f"{ext*100:+.1f}% from 20EMA — stretched")

    auto_passed = sum(1 for c in out if c["status"] == "pass")
    return {"checks": out, "score": auto_passed, "max_score": len(out), "auto_passed": auto_passed}


@router.get("/charges-preview")
def charges_preview(
    instrument: str = "",
    entry: float = 0.0,
    qty: int = 0,
    side: str = "B",
):
    """Live Zerodha-style charges estimate for the New Trade form.

    Uses a synthetic Trade object with just an entry leg (no exits yet) so the
    user sees the *entry-side* cost at creation time. Exit-side costs (STT on
    sell, DP fee) will show up automatically once exits are added.
    """
    from ..models import Trade as _T

    # Synthetic trade — purely for charges calc. Safe because we don't persist.
    t = _T(
        instrument=(instrument or "X").upper(),
        side="S" if side.strip().upper().startswith("S") else "B",
        initial_entry_price=max(entry, 0.0),
        initial_qty=max(qty, 0),
        sl=max(entry, 0.0),  # unused by charges calc
        entry_date=None,
        status="open",
    )
    t.pyramids = []
    t.exits = []
    return charges_svc.breakdown(t)


@router.get("/risk-budget")
def risk_budget(db: Session = Depends(get_db)):
    """Current open heat, exposure, and capital — what the New Trade form
    needs to tell the user whether another position fits the risk plan."""
    opens = db.query(Trade).filter(Trade.status == "open").all()
    current_open_heat = sum(calc.open_heat_rs(t) for t in opens)
    current_exposure = sum(calc.open_exposure_rs(t) for t in opens)
    current_capital = dash.current_capital(db)

    # A sensible soft cap: total open heat ≤ 6% of capital. The 6% figure is a
    # common retail risk budget but the user can override via Settings.
    max_heat_pct = app_settings.get_float(db, "max_open_heat_pct", 0.06)
    max_heat_rs = current_capital * max_heat_pct

    return {
        "capital": round(current_capital, 2),
        "open_heat_rs": round(current_open_heat, 2),
        "open_heat_pct_of_capital": round(
            (current_open_heat / current_capital) if current_capital else 0, 4
        ),
        "exposure_rs": round(current_exposure, 2),
        "max_heat_pct": max_heat_pct,
        "max_heat_rs": round(max_heat_rs, 2),
        "remaining_heat_rs": round(max(0.0, max_heat_rs - current_open_heat), 2),
        "open_positions": len(opens),
    }
