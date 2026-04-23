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

Single-user FastAPI + HTMX + Tailwind web app that replaces an Excel/Google-Sheets swing-trading journal (`Rushikesh's Trading Journal - 2025.xlsx`). The xlsx schema is the ground truth — any change to the `Trade` model must stay round-trippable with the importer.

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

## Conventions

- INR formatting uses Indian grouping (1,00,000 not 100,000) — the `inr` / `inr_signed` filters in `app/formatting.py`. Never hard-code `Rs` or `$`.
- Dates stored as SQL `Date`, never strings. Form inputs are `YYYY-MM-DD`; `trades.py::_parse_date` is the single parse point.
- Side is always the single char `'B'` or `'S'`. Forms normalize on POST.
- All router mutations return `RedirectResponse(url=..., status_code=303)` — never a JSON 200 for HTML form flows.

## Gotchas

- Jinja filters (`inr`, `pct_signed`, `pnl_color`, …) are registered in `formatting.register()` which is called by `deps.py`. A module that creates its own `Jinja2Templates` will 500 on those filters.
- `Trade.plan_followed` is a tri-state (`True`/`False`/`None`). Template form sends `"yes"` / `"no"` strings; the router maps them.
- The dashboard anchors trades to `close_date` when closed and `entry_date` when open — changing that changes the equity curve.
