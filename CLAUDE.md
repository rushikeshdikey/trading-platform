# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py           # serves http://127.0.0.1:8000 with reload
```

The SQLite database lives at `data/journal.db`. Deleting that file resets the app to a clean state (MasterList dropdowns re-seed on next boot).

## Architecture

Multi-user FastAPI + HTMX + Tailwind app: a swing-trading journal + scanner suite + daily decision-support cockpit for the Indian markets. Originally replaced an Excel/Google-Sheets journal (`Rushikesh's Trading Journal - 2025.xlsx`); the xlsx schema is still the ground truth for the `Trade` model.

Architecturally the app has grown into three loose layers:
1. **Journal core** — `Trade` / `Pyramid` / `Exit` / `CapitalEvent` / `MasterListItem`, the round-trippable xlsx surface.
2. **Scanner suite** — `app/scanner/`: bars cache (NSE+BSE bhavcopy), 7 detectors (HR, Trendline, Tight, Tightness, Institutional Buying, Base on Base, Minervini Trend Template), RS Rating, tight-SL picker.
3. **Decision-support surfaces** — `/cockpit` (Auto-Pilot picks + Market Mood), `/scanners` (unified results + funnel diagnostic), `/sector-rotation` (RRG), `/breadth`, `/scanners/ipos`. Public `/status` page for uptime monitoring.

### Request flow

- `app/main.py` — FastAPI app, mounts routers, auto-seeds MasterList on boot.
- `app/routers/*.py` — one file per feature (`trades`, `sizing`, `dashboard`, `masterlist_routes`, `settings_routes`, `imports`). All use form-encoded POSTs + `RedirectResponse` (Post/Redirect/Get); HTMX is used only for the position-sizer live-calc partial.
- `app/templates/*.html` — Jinja2. `base.html` is the shared shell (Tailwind/HTMX/Alpine/Chart.js via CDN). Partials live in `app/templates/partials/`.
- `app/deps.py` — the **one** `Jinja2Templates` instance every router imports. Registering filters on a second env is a footgun — all rendering must go through `app.deps.templates`.

### Data model (`app/models.py`)

- `Trade` — one row per position. `status` is `open` or `closed`; auto-flips to `closed` when `exits` fully cover `initial_qty + pyramids`.
- `Pyramid` / `Exit` — child tables (cascade-delete) so the xlsx's hard 2-pyramid / 3-exit cap becomes unlimited in the DB. UI shows the first N and lets the user "add another".
- `MasterListItem` — dropdown taxonomies (setup / proficiency / growth_area / exit_trigger / base_duration). Seeded from `app/masterlist.py::DEFAULTS` on first boot.
- `CapitalEvent` — deposits (positive) and withdrawals (negative). Dashboard applies them per-month.
- `Setting` — KV table (starting_capital, default risk %, default allocation %). `app/settings.py` has a `DEFAULTS` dict so the app works with an empty Settings table.

### Calculations (`app/calculations.py`)

All derived metrics (avg entry, P/L, R:R, stock move, open heat, sl %, etc.) are **pure functions** taking a `Trade` with its pyramids/exits eager-loaded. Never persist derived fields — always recompute. `metrics(trade)` returns a `TradeMetrics` dataclass that templates consume via `metrics_for[t.id]`.

Direction matters: for `side == 'S'` (short) stock-move and R:R are signed-flipped. Open-heat is always the rupee loss **if SL hits on the still-open qty** (zero once fully exited).

Position sizing has two methods:
- `size_by_risk(capital, risk_pct, entry, sl)` — risk per trade is bounded.
- `size_by_allocation(capital, allocation_pct, entry, sl)` — capital share per trade is bounded.

### Dashboard aggregation (`app/dashboard.py`)

`build_year(db, year)` walks every month of the chosen year, anchoring each trade's P/L to its **close month** (not entry month). It threads a `running_capital` through the months so the equity curve and "Final capital" compound correctly. Deposits/withdrawals land in their event month.

### xlsx importer (`app/importer.py`)

`import_from_xlsx(db, path)` is **destructive** — it deletes all `Trade`/`Pyramid`/`Exit` rows before importing. Column indices are hard-coded in the `COLUMNS` dict and must match the `DTrades` sheet layout. After import it backfills MasterList with any values seen in the sheet that weren't already there, and pulls the first non-zero `Starting capital` from `DDashboard` into `Setting.starting_capital`.

### Scanner suite (`app/scanner/`)

7 detectors registered in `patterns.py::SCAN_TYPES`:

- `horizontal_resistance` — cluster of prior highs, base age + freshness gates.
- `trendline_setup` — rising trendline through ≥3 swing lows.
- `tight_setup` — ATR(14)/close < 2.5%, range contraction.
- `tightness_trading` — Ankur Patel "Focus on one setup" 5-phase: strong upmove → base → low-vol → tight zone → buy points A (cheat at base support) / B (breakout at tight high). Hard gates for the SHAPE, scoring for magnitude. Each candidate tagged with `buy_point="A"|"B"|"—"`.
- `institutional_buying` — O'Neil-style accumulation: ≥6 up-days on volume ≥1.25× 50-DMA in 25 bars, in confirmed uptrend.
- `base_on_base` — two-stage continuation: prior base broke out, new base sitting above.
- `minervini_trend_template` — 8-criteria SEPA filter (50/150/200-DMA stacked + uptrending, 25% above 52w low + within 25% of 52w high, RS Rating ≥ 70).

Universe = bars cache minus ETFs (`universe.py`). No mcap or NSE-index gate at universe level — detectors do their own quality filtering via internal `_passes_liquidity` (MIN_BARS=60, MIN_PRICE=₹20, MIN_ADV20_RS=₹2 cr).

**RS Rating** (`rs_rating.py`) — IBD-style 1-99 percentile rank of weighted trailing returns (0.4×r3m + 0.2×r6m + 0.2×r9m + 0.2×r12m). Computed once per scan run, attached to every Candidate's extras, used as a hard gate by Minervini and as a score boost elsewhere.

**Tight-SL picker** (`tight_sl.py`) — universal SL chooser, applied uniformly in `runner._detect_one`. Default = PDL (last bar's low + 0.5% wick pad), tighter alternative if 3-bar low or 2×ATR(7) is closer to entry. **Never rejects** a candidate based on SL width; trader self-filters per-trade by reading the displayed SL%. See memory `feedback_no_sl_rejection.md`.

**Background workers** (all in daemon threads, spawned from in-process state):
- `bars_cache.start_background_refresh(lookback_days=380)` — bhavcopy backfill.
- `index_universe.start_background_refresh()` — NSE Total Market CSV.
- `runner.start_background_scan(user_id)` — Run-all-live (synchronous would pin gunicorn workers on the deep cache).
- `health_monitor.probe_and_log()` — APScheduler runs every 60s.

### Auto-Pilot daily picks (`app/auto_pilot.py`)

Reads the unified scan cache, surfaces top 1-3 A+ confluence trades on the cockpit's headline panel. Tier rule (canonical, in `routers/scanners.py::_compute_tier`):

- **A+** — 3 or more scanners agree.
- **A** — 2 scanners agree, OR a Minervini Trend Template hit (already an 8-criteria gate), OR single scanner with score ≥ 60 AND RS Rating ≥ 70.
- **B** — everything else.

Auto-Pilot only surfaces A+ tier (`_is_qualifying_tier`). When 0 picks qualify, panel renders "Stay in cash today" — empty state is intentional and matches the user's "trade less, trade solid" mantra.

### Public /status page

`/status` is unauthed (intentional — works even if login is broken). APScheduler probes /health every 60s, writes a `HealthCheck` row. Template renders 5-min buckets for last 24h + daily buckets for last 7d. Gaps in the row series ARE the downtime signal (when app is down, scheduler can't write either).

### Self-healing on boot (`app/main.py`)

- `_maybe_bootstrap_bars_cache()` — if `daily_bars` < 200k rows, kicks off 380-day bhavcopy backfill in a daemon thread. New deployments + catastrophic data loss recover automatically.
- `_prewarm_scanners_in_background()` — pre-computes the funnel diagnostic so first user request to `/scanners` is sub-100ms instead of 5-15s.

## Conventions

- INR formatting uses Indian grouping (1,00,000 not 100,000) — the `inr` / `inr_signed` filters in `app/formatting.py`. Never hard-code `Rs` or `$`.
- Dates stored as SQL `Date`, never strings. Form inputs are `YYYY-MM-DD`; `trades.py::_parse_date` is the single parse point.
- Side is always the single char `'B'` or `'S'`. Forms normalize on POST.
- All router mutations return `RedirectResponse(url=..., status_code=303)` — never a JSON 200 for HTML form flows.

## Gotchas

- Jinja filters (`inr`, `pct_signed`, `pnl_color`, …) are registered in `formatting.register()` which is called by `deps.py`. A module that creates its own `Jinja2Templates` will 500 on those filters.
- `Trade.plan_followed` is a tri-state (`True`/`False`/`None`). Template form sends `"yes"` / `"no"` strings; the router maps them.
- The dashboard anchors trades to `close_date` when closed and `entry_date` when open — changing that changes the equity curve.
- Detector signatures take `*, rs_rating=None, **_` so they accept the RS rating from `_detect_one` cleanly. New detectors must follow the same shape.
- Custom CSS classes (`.nav-link`, `.btn`, `.pill`) are wrapped in `:where()` in `base.html` to drop their specificity to 0 — Tailwind's `.hidden` and `.md:inline-flex` then win cleanly. Forgetting `:where()` makes desktop nav appear on mobile.
- SQLite stores all ints as int64; Postgres `Integer` is int32. `daily_bars.volume` was bumped to `BigInteger` because Indian high-volume names (ETFs, microcaps) post >2.1B daily volume. New numeric columns that could exceed int32 must use `BigInteger`.
- Synchronous external HTTP from a request handler will pin gunicorn workers on the slow path. Always background-thread fetches to NSE/BSE archives or yfinance/Kite. Pattern is `start_background_X()` in the relevant module + a status banner on the page.

## Production deployment

- One-VM AWS: t4g.medium EC2 in `ap-south-1`, EIP `3.6.133.188`, EBS gp3 (data persisted across instance replacements).
- docker-compose: app (gunicorn, 2 workers, 180s timeout, healthcheck) + postgres (16-alpine, healthcheck) + caddy (auto-TLS, lb_try_duration 30s).
- GitHub Actions (`.github/workflows/deploy.yml`) — native arm64 build on `ubuntu-24.04-arm`, OIDC into AWS, SSM `systemctl restart trading`. Steady-state deploy downtime: ~20 s.
- SSM access: `aws ssm start-session --target i-0bba78a7618f66040 --region ap-south-1`.
- For diagnostics, hit `/status` first — public, no auth.
