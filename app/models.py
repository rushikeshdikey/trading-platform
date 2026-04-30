from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
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

    # Kite-managed trade fields (Phase E1+). Populated when the trade is
    # placed via /trading/gtt/submit; null for manually-journaled trades.
    # The TSL daemon only acts on trades where kite_trigger_id is non-null
    # AND entry_status == 'filled'.
    kite_trigger_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Target leg of the OCO, kept fixed across SL ratchets so we only modify
    # the stop side. Null means we never set one (legacy trades).
    kite_target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Anchor used by the TSL ladder for trailing the SL. One of
    # "PDL" / "5EMA" / "10EMA" / null. Default to PDL on E1 submission;
    # the trader can override per-trade via /trades/<id>/edit.
    tsl_anchor: Mapped[str | None] = mapped_column(String, nullable=True)
    # Hybrid entry mode (Phase E1.1):
    #   'filled'  — position exists at the broker (default; covers manual
    #               journal trades + entry_mode='now' submissions).
    #   'pending' — GTT-single BUY trigger placed; entry will fill iff
    #               trigger price is touched. OCO bracket is placed by
    #               the TSL daemon at fill-resolution time.
    entry_status: Mapped[str] = mapped_column(
        String, nullable=False, default="filled", server_default="filled", index=True,
    )
    kite_buy_trigger_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True,
    )
    kite_buy_order_id: Mapped[str | None] = mapped_column(String, nullable=True)

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
    # Nullable in the deployed schema (post per-user-data scope migration);
    # the Python default fills it in on every insert.
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=datetime.utcnow,
    )


class NiftyDaily(Base):
    """Daily OHLCV of the Nifty 50 index (^NSEI on yfinance).

    Used for distribution-day counting (O'Neil's institutional pulse):
    days where the index closes -0.2% or worse on volume HIGHER than
    the prior session = institutional selling. >5 distribution days in
    25 sessions = market topping.

    Stored separately from daily_bars (which is constituent stocks)
    because the index has no symbol per-se — and yfinance fetches it
    via a different endpoint anyway.
    """

    __tablename__ = "nifty_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class HealthCheck(Base):
    """One row per scheduled internal /health probe.

    Drives the public /status page — lets users see uptime/downtime over
    the last 24h. Gaps in the timeline (no rows) implicitly mean the app
    was down for that minute (the scheduler couldn't write either).
    """

    __tablename__ = "health_check"

    id: Mapped[int] = mapped_column(primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Nullable in the deployed schema (server_default="0" backfills NULLs).
    # Code always populates this on insert, but the model has to match the
    # migration or `alembic check` flags drift.
    response_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, server_default="0", default=0,
    )
    error: Mapped[str | None] = mapped_column(String, nullable=True)


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
    # server_default matches the migration's existing_server_default so
    # `alembic check` doesn't see drift between model and Postgres schema.
    volume: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


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


class TslDecision(Base):
    """Append-only log of every TSL daemon decision.

    The daemon runs once per day after market close. For every open trade
    with a kite_trigger_id, it computes the current R-multiple, looks up
    the per-trade anchor (PDL / 5EMA / 10EMA), decides whether to ratchet
    the SL up, and records the outcome here — even when no action was
    taken (action='HOLD').

    Why we log no-action decisions too: forensic debugging. If the user
    asks "why didn't my SL move on day X for ABC?", we can answer it from
    this table without re-running the calculation.

    Composite unique index on (trade_id, scan_date) prevents the daemon
    from running twice on the same trade-day even if APScheduler fires
    twice (boot catchup + 15:50 cron); the second insert errors and the
    second run gracefully skips.
    """

    __tablename__ = "tsl_decision"
    __table_args__ = (
        Index(
            "ix_tsl_decision_trade_day",
            "trade_id", "decision_date",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trade_id: Mapped[int] = mapped_column(
        ForeignKey("trades.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    # Snapshot of the inputs the decision was made on.
    cmp: Mapped[float] = mapped_column(Float, nullable=False)
    current_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    anchor: Mapped[str | None] = mapped_column(String, nullable=True)
    anchor_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_stop: Mapped[float] = mapped_column(Float, nullable=False)
    proposed_stop: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Outcome.
    action: Mapped[str] = mapped_column(String, nullable=False, index=True)  # HOLD / MOVED_SL / ERROR
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Kite's response to modify_gtt, JSON-encoded. Null when action != MOVED_SL.
    modify_response: Mapped[str | None] = mapped_column(Text, nullable=True)


class BrokerAudit(Base):
    """Append-only audit log of every Kite Connect API call we make.

    The trading engine's foundation: when something goes wrong with real
    money we must be able to reconstruct exactly what request we sent,
    what came back, and how long it took. No retention — keep forever.
    Storage cost is trivial relative to the value of being able to answer
    "why did the SL get modified to X at HH:MM:SS?" months later.

    Indexed on (user_id, created_at) so a per-user timeline query is fast.
    """

    __tablename__ = "broker_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True
    )
    # Logical endpoint name (e.g. "holdings", "place_gtt", "modify_order").
    # NOT the URL — we wrap the kiteconnect SDK, not raw HTTP.
    endpoint: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Captured kwargs passed to the SDK method. JSON-encoded.
    request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Response from Kite. JSON-encoded. May be None on exception.
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # HTTP-ish status: 200 on success, the broker's error code otherwise,
    # or 0 if the call never reached Kite (network/SDK error).
    status: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Wall-clock latency in milliseconds.
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Exception class name + message when the call raised.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScanHistory(Base):
    """Append-only daily snapshot of scanner output.

    Why both this AND ScanCache: ScanCache is keyed only by scan_type — one
    row per scan_type, overwritten on every run. That means we can't replay
    "what was the universe yesterday?" — a major hole when tuning the
    composite scoring or auditing why a stock didn't surface.

    ScanHistory writes a fresh row per (scan_date, scan_type), so the full
    candidate list is preserved. 90-day retention is enforced by a janitor
    in runner._upsert_scan_cache (cheap, runs once per scan day).
    """

    __tablename__ = "scan_history"
    # Composite unique index on (scan_date, scan_type) — declared here so
    # alembic-autogenerate doesn't see drift against the migration that
    # creates ix_scan_history_date_type. Doubles as the DB-atomic mutex
    # for _maybe_eod_catchup (see app/main.py::_claim_eod_catchup).
    __table_args__ = (
        Index(
            "ix_scan_history_date_type",
            "scan_date", "scan_type",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    scan_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
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
