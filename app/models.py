from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_no: Mapped[int | None] = mapped_column(Integer, nullable=True)

    instrument: Mapped[str] = mapped_column(String, nullable=False, index=True)
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    option_type: Mapped[str | None] = mapped_column(String, nullable=True)  # CE/PE/FUT
    side: Mapped[str] = mapped_column(String, nullable=False)  # 'B' or 'S'

    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    initial_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    initial_qty: Mapped[int] = mapped_column(Integer, nullable=False)

    sl: Mapped[float] = mapped_column(Float, nullable=False)
    tsl: Mapped[float | None] = mapped_column(Float, nullable=True)
    cmp: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Total trading costs booked against this trade (STT + brokerage + exchange
    # + SEBI + stamp + GST + DP fee). Null means "use the estimator"; an
    # explicit 0 means "no charges" (rare).
    charges_rs: Mapped[float | None] = mapped_column(Float, nullable=True)

    setup: Mapped[str | None] = mapped_column(String, nullable=True)
    base_duration: Mapped[str | None] = mapped_column(String, nullable=True)

    plan_followed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exit_trigger: Mapped[str | None] = mapped_column(String, nullable=True)
    proficiency: Mapped[str | None] = mapped_column(String, nullable=True)
    growth_areas: Mapped[str | None] = mapped_column(String, nullable=True)
    observations: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[str] = mapped_column(String, default="open", index=True)
    close_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    pyramids: Mapped[list["Pyramid"]] = relationship(
        back_populates="trade",
        cascade="all, delete-orphan",
        order_by="Pyramid.sequence",
    )
    exits: Mapped[list["Exit"]] = relationship(
        back_populates="trade",
        cascade="all, delete-orphan",
        order_by="Exit.sequence",
    )


class Pyramid(Base):
    __tablename__ = "pyramids"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(
        ForeignKey("trades.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)

    trade: Mapped[Trade] = relationship(back_populates="pyramids")


class Exit(Base):
    __tablename__ = "exits"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(
        ForeignKey("trades.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)

    trade: Mapped[Trade] = relationship(back_populates="exits")


class MasterListItem(Base):
    __tablename__ = "masterlist_items"
    __table_args__ = (UniqueConstraint("category", "value", name="uq_category_value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class CapitalEvent(Base):
    """Deposits and withdrawals. Amount is positive for deposit, negative for withdrawal."""

    __tablename__ = "capital_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class ImportedExecution(Base):
    """Ledger of broker executions already imported.

    Used by the Zerodha import flow to skip duplicates on re-upload. Keyed by
    the broker's per-execution trade id, which is unique across the tradebook.
    """

    __tablename__ = "imported_executions"

    trade_id: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)  # 'B'/'S'
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    exchange: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, default="zerodha")
    applied_to_trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("trades.id", ondelete="SET NULL"), nullable=True
    )
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MarketBreadth(Base):
    """One row per trading day per universe — aggregated breadth stats."""

    __tablename__ = "market_breadth"
    __table_args__ = (UniqueConstraint("date", "universe", name="uq_breadth_date_universe"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    universe: Mapped[str] = mapped_column(String, nullable=False, default="nifty50")
    total_stocks: Mapped[int] = mapped_column(Integer, default=0)
    advances: Mapped[int] = mapped_column(Integer, default=0)
    declines: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    new_highs_52w: Mapped[int] = mapped_column(Integer, default=0)
    new_lows_52w: Mapped[int] = mapped_column(Integer, default=0)
    pct_above_20ema: Mapped[float] = mapped_column(Float, default=0.0)
    pct_above_50ema: Mapped[float] = mapped_column(Float, default=0.0)
    pct_above_200ema: Mapped[float] = mapped_column(Float, default=0.0)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class KiteInstrument(Base):
    """Cached row from Kite's instrument master (``kc.instruments(exchange)``).

    Lets us resolve a journal symbol to the correct ``EXCHANGE:TRADINGSYMBOL``
    for LTP calls, and powers symbol autocomplete on the new-trade form.
    Refreshed on login and on demand from the settings page.
    """

    __tablename__ = "kite_instruments"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_token: Mapped[int] = mapped_column(Integer, index=True)
    exchange_token: Mapped[int] = mapped_column(Integer, default=0)
    tradingsymbol: Mapped[str] = mapped_column(String, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    exchange: Mapped[str] = mapped_column(String, index=True, nullable=False)
    segment: Mapped[str | None] = mapped_column(String, nullable=True)
    instrument_type: Mapped[str | None] = mapped_column(String, nullable=True)
    lot_size: Mapped[int] = mapped_column(Integer, default=0)
    tick_size: Mapped[float] = mapped_column(Float, default=0.0)


class DailyBar(Base):
    """EOD OHLCV bar for a scanner universe symbol.

    Seeded from NSE bhavcopy (free, one HTTP per day covers all NSE equities).
    Unique on (symbol, date). Scanners read from here; nothing else depends on
    it, so safe to truncate and refresh.
    """

    __tablename__ = "daily_bars"
    __table_args__ = (UniqueConstraint("symbol", "date", name="uq_bar_symbol_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, default=0)


class Watchlist(Base):
    """User-curated watchlist. Rows land here from scanner results or manual add."""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    setup_label: Mapped[str | None] = mapped_column(String, nullable=True)
    alert_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    suggested_sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ScanRun(Base):
    """History of scanner runs (for UI 'last run at' and debugging)."""

    __tablename__ = "scan_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    scan_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    universe_size: Mapped[int] = mapped_column(Integer, default=0)
    candidates_count: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    bars_refreshed: Mapped[int] = mapped_column(Integer, default=0)


class InstrumentMeta(Base):
    """Per-symbol fundamentals cache (market cap, TTL-refreshed).

    Populated from yfinance ``fast_info.market_cap`` in a threaded batch.
    Scanner uses this to gate the universe to investable mid/large caps —
    without it, BSE-exclusive corporate bonds and illiquid small caps would
    otherwise sneak past the liquidity filter.
    """

    __tablename__ = "instrument_meta"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    market_cap_rs: Mapped[float | None] = mapped_column(Float, nullable=True)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)


class InstrumentPrice(Base):
    """Cache for resolved Yahoo Finance suffix + last quote per instrument.

    Keyed by the journal's instrument symbol (uppercase). `yf_suffix` is the
    exchange suffix that successfully returned a price (.NS, .BO, etc.), so
    subsequent refreshes skip re-probing exchanges.
    """

    __tablename__ = "instrument_prices"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    yf_suffix: Mapped[str | None] = mapped_column(String, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    prev_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
