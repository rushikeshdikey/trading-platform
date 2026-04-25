"""Market breadth analytics — segmented by market-cap.

Definition:
  Breadth = what fraction of a reference universe of stocks is participating
  in a move. Segmenting by cap (large / mid / small) reveals leadership
  rotation: the Nifty can look strong while small caps quietly weaken
  (classic late-bull warning), or vice-versa at bottoms.

Metrics per (date, universe):
  - Advances, Declines, Unchanged
  - 52-week New Highs / New Lows
  - % above 20 / 50 / 200 day EMA

Universes stored: ``large`` (Nifty 100), ``mid`` (Nifty Midcap 100),
``small`` (Nifty Smallcap 100), ``all`` (union of the three ≈ 300 stocks).
Legacy ``nifty50`` rows are still readable but no longer written.

Data source: yfinance batched ``download`` — one HTTP call for the full
union; segment breadth is then computed in-memory from that single payload.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from .models import MarketBreadth

log = logging.getLogger("journal.breadth")

# ---------------------------------------------------------------------------
# Universes. Constituents drift quarterly; these are close-enough snapshots.
# A ticker not found on yfinance is silently skipped, so staleness downgrades
# sample size rather than breaking the page.
# ---------------------------------------------------------------------------

LARGE_CAP: tuple[str, ...] = (
    # Nifty 50
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "BHARTIARTL", "INFY",
    "ITC", "SBIN", "LT", "HINDUNILVR", "BAJFINANCE", "KOTAKBANK",
    "AXISBANK", "MARUTI", "HCLTECH", "SUNPHARMA", "M&M", "ONGC",
    "NTPC", "TATAMOTORS", "TITAN", "WIPRO", "ULTRACEMCO", "ASIANPAINT",
    "NESTLEIND", "POWERGRID", "JSWSTEEL", "TATASTEEL", "ADANIPORTS",
    "GRASIM", "HINDALCO", "COALINDIA", "BAJAJFINSV", "BAJAJ-AUTO",
    "TECHM", "CIPLA", "DRREDDY", "INDUSINDBK", "BRITANNIA", "EICHERMOT",
    "HEROMOTOCO", "SBILIFE", "BPCL", "DIVISLAB", "APOLLOHOSP",
    "HDFCLIFE", "TATACONSUM", "LTIM", "ADANIENT", "SHRIRAMFIN",
    # Nifty Next 50
    "ADANIGREEN", "ADANIPOWER", "AMBUJACEM", "ATGL", "BAJAJHLDNG",
    "BANKBARODA", "BEL", "BOSCHLTD", "CANBK", "CHOLAFIN", "COLPAL",
    "DABUR", "DLF", "DMART", "GAIL", "GODREJCP", "HAL", "HAVELLS",
    "HDFCAMC", "ICICIGI", "ICICIPRULI", "INDIGO", "IOC", "IRCTC",
    "IRFC", "JINDALSTEL", "LICI", "MARICO", "MOTHERSON", "MUTHOOTFIN",
    "NAUKRI", "PIDILITIND", "PFC", "PNB", "POLICYBZR", "RECLTD",
    "SAIL", "SIEMENS", "SRF", "TATAPOWER", "TORNTPHARM", "TRENT",
    "TVSMOTOR", "VEDL", "ZOMATO", "ZYDUSLIFE", "LODHA", "ABB",
    "CUMMINSIND", "SHREECEM",
)

MID_CAP: tuple[str, ...] = (
    "ASHOKLEY", "AUBANK", "AUROPHARMA", "BALKRISIND", "BANDHANBNK",
    "BERGEPAINT", "BHARATFORG", "BHEL", "BIOCON", "COFORGE",
    "CONCOR", "DEEPAKNTR", "DIXON", "ESCORTS",
    "EXIDEIND", "FEDERALBNK", "GLAND", "GLENMARK", "GMRAIRPORT",
    "GODREJPROP", "GUJGASLTD", "HINDPETRO", "IDFCFIRSTB",
    "INDHOTEL", "INDUSTOWER", "IPCALAB", "JSL", "JSWENERGY",
    "JUBLFOOD", "LAURUSLABS", "LICHSGFIN", "LINDEINDIA", "LUPIN",
    "MAXHEALTH", "MFSL", "MPHASIS", "MRF", "NHPC", "NMDC",
    "OBEROIRLTY", "OFSS", "PAGEIND", "PATANJALI", "PEL",
    "PERSISTENT", "PETRONET", "PIIND", "POLYCAB", "PRESTIGE",
    "SUNTV", "SUPREMEIND", "SYNGENE", "TATACOMM", "TATAELXSI",
    "TIINDIA", "TORNTPOWER", "UBL", "UPL", "VOLTAS", "YESBANK",
    "ABBOTINDIA", "ACC", "ALKEM", "APOLLOTYRE", "ASTRAL",
    "BHARATHEAVY", "CROMPTON", "CUB", "DALBHARAT", "DELTACORP",
    "EMAMILTD", "ENDURANCE", "FLUOROCHEM", "GNFC", "GODREJIND",
    "GRAPHITE", "HATSUN", "HONAUT", "IDEA", "IGL", "IRB",
    "ISEC", "KAJARIACER", "KANSAINER", "KEI", "LAXMIMACH",
    "MANAPPURAM", "MGL", "NBCC", "NAM-INDIA", "NATIONALUM",
    "OIL", "PFIZER", "PHOENIXLTD", "RAMCOCEM", "RBLBANK",
    "SANOFI", "SCHAEFFLER", "SUNDRMFAST",
)

SMALL_CAP: tuple[str, ...] = (
    "AARTIIND", "AAVAS", "ABFRL", "AFFLE", "AJANTPHARM", "AKZOINDIA",
    "AMARAJABAT", "ANGELONE", "APLLTD", "APTUS", "ASAHIINDIA",
    "ASTERDM", "ATUL", "BAJAJELEC", "BALAMINES", "BATAINDIA",
    "BBTC", "BDL", "BEML", "BIKAJI", "BIRLACORPN", "BLUEDART",
    "BLUESTARCO", "BSE", "BSOFT", "CAMS", "CAPLIPOINT",
    "CARBORUNIV", "CASTROLIND", "CCL", "CDSL", "CEATLTD",
    "CENTRALBK", "CENTURYTEX", "CERA", "CESC", "CHAMBLFERT",
    "CHENNPETRO", "CMSINFO", "CREDITACC", "CRISIL", "CYIENT",
    "DBL", "DCBBANK", "DEEPAKFERT", "DELHIVERY", "DHANUKA",
    "EASEMYTRIP", "EIDPARRY", "EIHOTEL", "ELGIEQUIP", "EPL",
    "ERIS", "FINCABLES", "FINEORG", "FORTIS", "FSL", "GESHIP",
    "GILLETTE", "GLAXO", "GLS", "GODFRYPHLP", "GPIL", "GRANULES",
    "GRINDWELL", "GRSE", "GSFC", "GSPL", "HAPPSTMNDS", "HEG",
    "HFCL", "HINDCOPPER", "HONASA", "HSCL", "ICRA", "IDBI",
    "IFBIND", "IIFL", "INDIACEM", "INDIAMART", "INDIANB",
    "INOXWIND", "INTELLECT", "IOB", "JBCHEPHARM", "JCHAC",
    "JINDALSAW", "JKCEMENT", "JKPAPER", "JMFINANCIL", "JPASSOCIAT",
    "JUSTDIAL", "KALYANKJIL", "KARURVYSYA", "KEC", "KIMS",
    "KIRLOSENG", "KNRCON", "KPITTECH", "KPRMILL", "KRBL",
)


ALL_SEGMENTS = {
    "large": LARGE_CAP,
    "mid": MID_CAP,
    "small": SMALL_CAP,
}

UNIVERSE_LABELS = {
    "all": "All caps",
    "large": "Large cap",
    "mid": "Mid cap",
    "small": "Small cap",
}


def universe_symbols(universe: str) -> tuple[str, ...]:
    """Resolve a universe name to its ticker tuple (no .NS suffix)."""
    if universe == "all":
        seen: dict[str, None] = {}
        for segment in ALL_SEGMENTS.values():
            for s in segment:
                seen.setdefault(s, None)
        return tuple(seen.keys())
    return ALL_SEGMENTS.get(universe, LARGE_CAP)


def _ema(series, span: int):
    """Standard exponential moving average via pandas."""
    return series.ewm(span=span, adjust=False).mean()


def _fetch_universe_ohlc(symbols: Iterable[str], days: int = 260):
    """Download daily OHLC for N symbols as one batched yfinance call.

    Returns a pandas multi-level DataFrame (``(field, TICKER.NS)``).
    Silently drops unresolvable tickers.
    """
    import yfinance as yf

    tickers = " ".join(f"{s}.NS" for s in symbols)
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=days)
    data = yf.download(
        tickers,
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1d",
        group_by="ticker",
        progress=False,
        auto_adjust=False,
        threads=True,
    )
    return data


def _safe_ticker_frame(data, sym: str):
    """Pull one ticker's OHLC frame out of the multi-index download."""
    key = f"{sym}.NS"
    try:
        if key in data.columns.levels[0]:
            df = data[key].dropna(how="all")
            return df if not df.empty else None
    except (KeyError, AttributeError):
        pass
    return None


def _aggregate(frames: dict, symbols: Iterable[str], all_dates: list) -> list[dict]:
    """Walk every trading date and collapse member symbols into breadth stats.

    ``frames`` is a symbol → prepared DataFrame map (Close, prev_close, EMAs,
    52w high/low already computed). Returns one dict per trading date.
    """
    import math

    out: list[dict] = []
    member_frames = {s: frames[s] for s in symbols if s in frames}

    for ts in all_dates:
        d = ts.date() if hasattr(ts, "date") else ts
        advances = declines = unchanged = 0
        new_highs = new_lows = 0
        above_20 = above_50 = above_200 = valid = 0

        for df in member_frames.values():
            if ts not in df.index:
                continue
            row = df.loc[ts]
            close = row.get("Close")
            prev = row.get("prev_close")
            if close is None or math.isnan(close):
                continue
            valid += 1
            if prev is not None and not math.isnan(prev):
                if close > prev:
                    advances += 1
                elif close < prev:
                    declines += 1
                else:
                    unchanged += 1
            for key in ("ema20", "ema50", "ema200"):
                v = row.get(key)
                if v is not None and not math.isnan(v) and close > v:
                    if key == "ema20":
                        above_20 += 1
                    elif key == "ema50":
                        above_50 += 1
                    else:
                        above_200 += 1
            h52 = row.get("high52")
            l52 = row.get("low52")
            if h52 is not None and not math.isnan(h52) and close >= h52 - 1e-6:
                new_highs += 1
            if l52 is not None and not math.isnan(l52) and close <= l52 + 1e-6:
                new_lows += 1

        if valid == 0:
            continue
        pct = lambda n: round(100.0 * n / valid, 2)
        out.append({
            "date": d,
            "total_stocks": valid,
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "new_highs_52w": new_highs,
            "new_lows_52w": new_lows,
            "pct_above_20ema": pct(above_20),
            "pct_above_50ema": pct(above_50),
            "pct_above_200ema": pct(above_200),
        })
    return out


def compute_and_store(db: Session, days: int = 130) -> dict:
    """Fetch the full union once, compute + upsert breadth for every segment.

    ``days`` is the number of recent trading days to refresh; the fetch
    window is automatically padded so 200-day EMAs and 52-week high/lows
    have enough history.
    """
    import math
    import pandas as pd

    union = universe_symbols("all")
    data = _fetch_universe_ohlc(union, days=max(days + 220, 280))

    frames: dict = {}
    for s in union:
        df = _safe_ticker_frame(data, s)
        if df is None or "Close" not in df or len(df) < 20:
            continue
        df = df.copy()
        df["prev_close"] = df["Close"].shift(1)
        df["ema20"] = _ema(df["Close"], 20)
        df["ema50"] = _ema(df["Close"], 50)
        df["ema200"] = _ema(df["Close"], 200)
        df["high52"] = df["High"].rolling(252, min_periods=30).max()
        df["low52"] = df["Low"].rolling(252, min_periods=30).min()
        frames[s] = df

    if not frames:
        return {"rows_written": 0, "reason": "no data from yfinance"}

    all_ts: set = set()
    for df in frames.values():
        all_ts.update(df.index[-days:])
    all_ts = sorted(all_ts)

    total_rows = 0
    per_universe: dict[str, int] = {}
    universes = {
        "all": list(frames.keys()),
        "large": [s for s in LARGE_CAP if s in frames],
        "mid": [s for s in MID_CAP if s in frames],
        "small": [s for s in SMALL_CAP if s in frames],
    }

    for uni, members in universes.items():
        rows = _aggregate(frames, members, all_ts)
        for r in rows:
            existing = (
                db.query(MarketBreadth)
                .filter(MarketBreadth.date == r["date"], MarketBreadth.universe == uni)
                .first()
            )
            if existing is None:
                existing = MarketBreadth(date=r["date"], universe=uni)
                db.add(existing)
            existing.total_stocks = r["total_stocks"]
            existing.advances = r["advances"]
            existing.declines = r["declines"]
            existing.unchanged = r["unchanged"]
            existing.new_highs_52w = r["new_highs_52w"]
            existing.new_lows_52w = r["new_lows_52w"]
            existing.pct_above_20ema = r["pct_above_20ema"]
            existing.pct_above_50ema = r["pct_above_50ema"]
            existing.pct_above_200ema = r["pct_above_200ema"]
            existing.computed_at = datetime.utcnow()
            total_rows += 1
        per_universe[uni] = len(rows)

    db.commit()
    return {
        "rows_written": total_rows,
        "symbols_seen": len(frames),
        "per_universe": per_universe,
    }


def mood_score(row) -> dict:
    """Composite 0-100 market mood, computed from a MarketBreadth row.

    Inspired by algoxr's Market Mood panel. Uses inputs we already track —
    no external API calls required. Weights chosen so long-term trend
    dominates short-term noise (the score should answer "is this a market
    where adding risk pays off this week?").

      - pct_above_200ema   (trend health, long)         × 0.35
      - pct_above_50ema    (trend health, medium)       × 0.30
      - pct_above_20ema    (momentum, short)            × 0.15
      - adv / (adv + dec)  (today's participation, %)   × 0.10
      - 52W new-high share (NH / (NH + NL), %)          × 0.10

    Returns score, tier label, color class, and a per-component breakdown
    so the UI can show a gauge AND explain why.
    """
    if row is None:
        return {"score": None, "label": "—", "klass": "bg-zinc-100 text-zinc-500 border-zinc-200", "components": []}

    advances = (row.advances or 0)
    declines = (row.declines or 0)
    nh = (row.new_highs_52w or 0)
    nl = (row.new_lows_52w or 0)

    # 0-100 floats for each component.
    c_200 = float(row.pct_above_200ema or 0.0)
    c_50 = float(row.pct_above_50ema or 0.0)
    c_20 = float(row.pct_above_20ema or 0.0)
    adv_share = (advances / (advances + declines) * 100.0) if (advances + declines) > 0 else 50.0
    nh_share = (nh / (nh + nl) * 100.0) if (nh + nl) > 0 else 50.0

    score = (
        c_200 * 0.35
        + c_50 * 0.30
        + c_20 * 0.15
        + adv_share * 0.10
        + nh_share * 0.10
    )
    score = max(0.0, min(100.0, score))

    # Tiers — same buckets you'd actually trade off of.
    if score >= 75:
        label, klass = "Euphoric — protect gains", "bg-amber-100 text-amber-900 border-amber-200"
    elif score >= 60:
        label, klass = "Risk-on — full size OK", "bg-emerald-100 text-emerald-800 border-emerald-300"
    elif score >= 45:
        label, klass = "Constructive — selective adds", "bg-emerald-50 text-emerald-700 border-emerald-200"
    elif score >= 30:
        label, klass = "Cautious — half size", "bg-zinc-100 text-zinc-700 border-zinc-200"
    elif score >= 15:
        label, klass = "Risk-off — defensive", "bg-rose-50 text-rose-700 border-rose-200"
    else:
        label, klass = "Bearish — sit on hands", "bg-rose-100 text-rose-800 border-rose-200"

    components = [
        {"key": "above_200", "label": "% above 200 EMA", "value": round(c_200, 1), "weight": 35},
        {"key": "above_50",  "label": "% above 50 EMA",  "value": round(c_50, 1),  "weight": 30},
        {"key": "above_20",  "label": "% above 20 EMA",  "value": round(c_20, 1),  "weight": 15},
        {"key": "adv_share", "label": "Adv / (Adv+Dec)", "value": round(adv_share, 1), "weight": 10},
        {"key": "nh_share",  "label": "52W NH / (NH+NL)", "value": round(nh_share, 1), "weight": 10},
    ]

    return {
        "score": round(score, 1),
        "label": label,
        "klass": klass,
        "components": components,
        "as_of": row.date,
    }


def sentiment_label(pct_above_200: float, pct_above_50: float) -> tuple[str, str]:
    """Human-friendly sentiment tag + a Tailwind color class."""
    composite = (pct_above_200 + pct_above_50) / 2
    if composite >= 70:
        return "Strongly Bullish", "bg-emerald-100 text-emerald-800 border-emerald-200"
    if composite >= 55:
        return "Bullish", "bg-emerald-50 text-emerald-700 border-emerald-200"
    if composite >= 45:
        return "Moderately Bullish", "bg-emerald-50 text-emerald-700 border-emerald-100"
    if composite >= 35:
        return "Neutral", "bg-zinc-100 text-zinc-700 border-zinc-200"
    if composite >= 20:
        return "Bearish", "bg-rose-50 text-rose-700 border-rose-200"
    return "Strongly Bearish", "bg-rose-100 text-rose-800 border-rose-200"


def series_for(db: Session, days: int, universe: str = "all") -> list[MarketBreadth]:
    cutoff = date.today() - timedelta(days=days)
    return (
        db.query(MarketBreadth)
        .filter(MarketBreadth.universe == universe, MarketBreadth.date >= cutoff)
        .order_by(MarketBreadth.date.asc())
        .all()
    )


def latest(db: Session, universe: str = "all") -> MarketBreadth | None:
    return (
        db.query(MarketBreadth)
        .filter(MarketBreadth.universe == universe)
        .order_by(MarketBreadth.date.desc())
        .first()
    )
