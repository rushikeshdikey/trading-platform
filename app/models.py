from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
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
    __table_args__ = (
        UniqueConstraint("user_id", "category", "value", name="uq_user_category_value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String, nullable=False, index=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class CapitalEvent(Base):
    """Deposits and withdrawals. Amount is positive for deposit, negative for withdrawal."""

    __tablename__ = "capital_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class ImportedExecution(Base):
    """Ledger of broker executions already imported.

    Used by the Zerodha import flow to skip duplicates on re-upload. Keyed by
    the broker's per-execution trade id, which is unique across the tradebook.
    Composite PK (user_id, trade_id) so two users can have the same broker
    trade ID without collision (each user has their own broker account).
    """

    __tablename__ = "imported_executions"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
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
    # BigInteger because Indian high-volume names (ETFs, micro-caps) can
    # post >2.1B daily volume — overflows Postgres INT32. SQLite stores
    # ints as 64-bit always so local doesn't notice; prod hits this.
    volume: Mapped[int] = mapped_column(BigInteger, default=0)


class Watchlist(Base):
    """User-curated watchlist. Rows land here from scanner results or manual add."""

    __tablename__ = "watchlist"
    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_watchlist_user_symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String, nullable=False, index=True)
    setup_label: Mapped[str | None] = mapped_column(String, nullable=True)
    alert_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    suggested_sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ScanRun(Base):
    """History of scanner runs (per user) — for the UI 'last run' indicator
    and debugging. Result payloads live in the SHARED ``ScanCache`` so the
    background pre-warm doesn't have to pick a user.
    """

    __tablename__ = "scan_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    scan_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    universe_size: Mapped[int] = mapped_column(Integer, default=0)
    candidates_count: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    bars_refreshed: Mapped[int] = mapped_column(Integer, default=0)


class ScanCache(Base):
    """Shared scanner-result cache — one row per scan_type, upserted by
    whatever ran the scan most recently (UI button or EOD pre-warm).
    Universe data, not user data — no user_id, every user reads the same.
    """

    __tablename__ = "scan_cache"

    scan_type: Mapped[str] = mapped_column(String, primary_key=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    universe_size: Mapped[int] = mapped_column(Integer, default=0)
    candidates_count: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


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


class User(Base):
    """Single account that logs into the app.

    Onboarding is admin-driven: an existing admin creates the user with a
    temp password, shares it out-of-band, the user logs in and changes it.
    No public signup form. Email is the login identifier and is stored
    lower-cased.

    Per-user Kite credentials live as encrypted blobs (Fernet, key derived
    from `SECRET_KEY`). Decryption helpers live in `app/auth.py`.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    # Per-user Kite Connect credentials. Each user supplies their own Kite
    # developer-app keys. Phase 3 wires these into kite.py.
    kite_api_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    kite_api_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    kite_access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    kite_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
