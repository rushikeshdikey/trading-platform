# Trading Journal

A self-hosted swing-trading platform for the Indian markets — journal, position sizer, EOD scanner suite, watchlist, and a daily trading cockpit, all in one FastAPI app.

It started as a one-for-one replacement for the "copy this Google Sheet" workflow and grew into the daily decision-support tool the spreadsheet couldn't be: live capital tracking, conviction-scored signals across NSE + BSE, and a one-click flow from "this looks like a setup" to a fully sized, journaled trade.

**Stack:** FastAPI · SQLAlchemy · Alembic · SQLite (dev) / Postgres (prod) · Jinja2 · Tailwind · HTMX · Alpine.js · Chart.js · APScheduler.

---

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # edit if you want Kite / a non-default secret
.venv/bin/python run.py       # http://127.0.0.1:8000
```

On first boot the app:

1. Runs Alembic migrations against `data/journal.db`.
2. Seeds an empty SQLite DB.
3. Sends you to **`/setup`** to create the bootstrap admin account. After that, `/setup` is dead.

Subsequent boots drop you on the **Cockpit** at `/cockpit`.

If you have an existing journal, head to **Import** and either upload an xlsx that matches the `DTrades` layout or pull the repo-bundled file in one click. Import is destructive — it replaces all trades — so use it on day zero, not for incremental edits.

---

## What's in it

### Daily Trading Cockpit `/cockpit`
The landing page. Five-panel decision support designed to answer "what should I do today?":
- **Market verdict** — NSE breadth (advance/decline, % above 50/200 DMA) condensed to a single risk-on / risk-off call.
- **Open-position actions** — every open trade with live heat, days held, and a suggested action (trail, exit, scale).
- **Conviction-scored signals** — best fresh setups across all 4 scanners, deduplicated and tiered A+ / A / B.
- **Risk budget** — capital, open heat, today's available R, max-positions cap.
- **Personal edge** — your historical win rate by setup so you don't take the trades that burn you.

### Scanners `/scanners`
EOD swing-setup screener across a two-tier universe: the **Nifty Total Market** index (~750 mid+ caps) plus any non-index name with bars that clears a basic liquidity floor (last close ≥ ₹30, 20-day avg turnover ≥ ₹2 cr). This way real smallcap setups like STYLAMIND don't silently disappear just because NSE's index didn't include them. Bars fetched daily at 15:35 IST (APScheduler) from NSE + BSE bhavcopy and cached in `DailyBar`; results pre-computed and cached in `ScanCache` so the page renders in <50 ms.

Four detectors:

| Scanner | What it looks for |
|---|---|
| **Horizontal Resistance** | Cluster of prior highs in a 2 % band, ≥ 2 touches spaced ≥ 15 days, current close within 5 % below. Base age + freshness + depth + recent-coil gates enforced. |
| **Trendline Setup** | Rising trendline through ≥ 3 swing lows, R² ≥ 0.92, price within 3 % of the line today. |
| **Tight Setup** | ATR(14)/close < 2.5 %, 20-bar range < 10 %, ATR(14) contracting vs ATR(50). |
| **Tightness Trading** | 5-phase Ankur-Patel setup: strong upmove → base → low-vol dry-up → tight range ≤ 6 % → buy points A / B. |

Each candidate is risk-sized at two tiers (0.25 % and 0.5 % of current capital, configurable), and one click takes the row to the new-trade form pre-filled with entry / SL / qty.

### Watchlist `/watchlist`
Manual or one-click-from-scanner list of symbols you want to track. Stores alert price, setup label, suggested SL, and notes. Bulk-add from scanner results.

### Breadth `/breadth`
NSE breadth dashboard — advance/decline ratios, % above moving averages, sectoral participation. Used by the Cockpit's market-verdict panel and useful on its own when you want context before pressing the buy button.

### Trades `/trades`
- Open / closed / all views.
- Unlimited pyramids and scale-outs (the xlsx caps at 2 / 3; the DB doesn't).
- Auto-flips to closed when exits cover the full position.
- Per-trade detail page: P/L, R:R, stock move, open heat, plan-followed (yes/no/none), notes.

### Position sizer `/sizing`
Both methods side-by-side, live as you type:
- **Risk-on-capital** — risk per trade is bounded.
- **Allocation-based** — capital share per trade is bounded.

### Dashboard `/dashboard`
Performance review (not the daily decision tool — that's the Cockpit).
Year P/L, equity curve, monthly bars, win %, avg R:R, best/worst month, setup-wise performance, deposits/withdrawals applied per close month.

### Lists `/masterlist`
Editable dropdown taxonomies — Setup / Proficiency / Growth area / Exit trigger / Base duration. Per-user; seeded from `app/masterlist.py::DEFAULTS` on user creation.

### Settings `/settings`
Starting capital, default risk %, default allocation %, deposits/withdrawals ledger, Kite API credentials (encrypted at rest with a per-user key derived from the session secret).

### Import `/import`
xlsx → DB. Hard-coded column indices in `app/importer.py::COLUMNS` must match the `DTrades` sheet. After import, MasterList is backfilled with any unseen values and `Setting.starting_capital` is pulled from `DDashboard`.

### Live prices (Kite)
Optional Zerodha Kite Connect integration. When configured, a background refresher pulls last-traded prices for every open position so the Cockpit's "open heat" is real-time, not stale-from-bhavcopy.

---

## Architecture in 30 seconds

- **`app/main.py`** — FastAPI app, middleware (sessions + per-request user contextvar), router wiring, exception handlers (404 page, login redirect), boots the bars-cache scheduler.
- **`app/routers/*.py`** — one file per feature. All form-encoded POSTs use the Post/Redirect/Get pattern. HTMX is used only where partial re-renders matter (sizer, scanner filter sidebar).
- **`app/templates/*.html`** — Jinja2. `base.html` is the shared shell. `404.html` is the trader-themed not-found page.
- **`app/deps.py`** — the **one** `Jinja2Templates` instance every router imports. Custom filters (`inr`, `inr_signed`, `pct_signed`, `pnl_color`, …) are registered here.
- **`app/models.py`** — SQLAlchemy: `User`, `Trade` + `Pyramid` + `Exit`, `Watchlist`, `MasterListItem`, `CapitalEvent`, `Setting`, `DailyBar`, `ScanCache`, `ScanRun`, `MarketCap`.
- **`app/calculations.py`** — pure functions for derived metrics. **Never persist derived fields; always recompute.**
- **`app/scanner/`** — bars cache (bhavcopy fetch + upsert), pattern detectors, runner (parallel), risk sizing, sparkline SVGs, NSE index-list universe.
- **`app/auth.py`** — session-cookie auth, password hashing (passlib), per-request user contextvar consumed by `orm_events.py` for automatic per-user query filtering.
- **`alembic/`** — schema migrations. Run automatically on boot.

---

## Multi-user

Originally single-user; now supports any number of accounts on one deployment.

- Bootstrap admin is created via `/setup` (only available when no users exist).
- Admins manage users at `/admin/users`.
- Every query is auto-filtered by `user_id` via SQLAlchemy ORM events — no router has to remember to scope.
- Kite credentials are per-user, encrypted at rest.
- Sessions are signed cookies (HMAC with `SECRET_KEY`). Rotating the key invalidates every session.

---

## Configuration

`.env` at the project root (gitignored). See `.env.example` for the full list.

| Variable | Required | Purpose |
|---|---|---|
| `ENV` | recommended | `dev` (default) or `prod`. Prod refuses to boot with weak secrets or SQLite. |
| `SECRET_KEY` | prod | ≥ 32 char random value. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `DATABASE_URL` | prod | SQLAlchemy URL. Dev defaults to `sqlite:///data/journal.db`; prod must be Postgres. |
| `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_REDIRECT_URL` | optional | Per-user values override these from `/settings`. |

---

## Deploying

The repo ships everything for a single-VM AWS deploy:

```
docker-compose.yml         App + Caddy (TLS) + Postgres
Dockerfile                 Multi-stage Python build, gunicorn + uvicorn workers
Caddyfile                  Auto-TLS via Let's Encrypt
infra/terraform/           One-VM AWS stack (EC2 + EIP + R53 + IAM OIDC for CI)
infra/scripts/bootstrap.sh One-shot host setup (Docker, fail2ban, log rotation)
.github/workflows/ci.yml   pytest + ruff on every push
.github/workflows/deploy.yml  OIDC → EC2 SSM → docker compose pull + up on main merges
```

To stand it up fresh:

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # set domain, instance type, etc.
terraform init && terraform apply
# Once the EC2 is up, GitHub Actions takes over on push-to-main.
```

Production currently runs at <https://trading.zilionix.com>.

---

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Coverage focuses on the load-bearing bits — calculations, auth, per-user data isolation, cockpit actions, HTTP request isolation. UI is verified manually.

---

## Conventions worth knowing

- **INR formatting** uses Indian grouping (1,00,000 not 100,000) — the `inr` / `inr_signed` Jinja filters. Never hard-code `Rs` or `$`.
- **Dates** stored as SQL `Date`, never strings. Form inputs are `YYYY-MM-DD`; `trades.py::_parse_date` is the single parse point.
- **Side** is always the single char `'B'` or `'S'`. Forms normalize on POST. `Trade.plan_followed` is tri-state (`True` / `False` / `None`).
- **Direction matters in metrics.** For `side == 'S'`, stock-move and R:R are signed-flipped. Open-heat is always the rupee loss if SL hits on the still-open qty (zero once fully exited).
- **Dashboard anchors trades to `close_date` when closed and `entry_date` when open.** Changing that changes the equity curve.
- All router mutations return `RedirectResponse(url=..., status_code=303)` — never JSON 200 for HTML form flows.

---

## Data is your data

Everything lives in `data/journal.db` (dev) or your Postgres instance (prod). Back it up by copying the file or `pg_dump`ing the DB. There is no proprietary lock-in: the schema is documented above, the importer round-trips with the source xlsx, and migrating SQLite → Postgres is one `DATABASE_URL` change away.
