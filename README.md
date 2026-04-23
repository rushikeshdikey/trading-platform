# Trading Journal

A clean, user-friendly trading journal for Indian swing traders — a drop-in replacement for the classic "copy this Google Sheet" workflow.

Built on: FastAPI, SQLAlchemy (SQLite), Jinja2, Tailwind, HTMX, Alpine.js, Chart.js.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
# open http://127.0.0.1:8000
```

On first run:
1. Go to **Import** and click "Import from repo file" to pull in your existing `Rushikesh's Trading Journal - 2025.xlsx`.
2. Or click **+ New trade** and log one by hand — the 3-step wizard (entry → size → notes) handles the math for you.

## What's in it

- **Dashboard** — year P/L, equity curve, monthly bars, win %, avg R:R, best/worst month, setup-wise performance, open positions with live heat.
- **Trades** — open / closed / all, one click to drill into a trade. Supports unlimited pyramids and scale-outs; auto-closes when you exit the full qty.
- **Position sizer** — both methods (risk-on-capital and allocation-based) side by side, live as you type.
- **Lists** — editable dropdowns for Setup / Proficiency / Growth area / Exit trigger / Base duration.
- **Settings** — starting capital, default risk %, default allocation %, plus a deposits/withdrawals ledger that flows into the monthly dashboard.
- **Import** — one click to replace app data with your existing xlsx; or upload any xlsx with the same `DTrades` column layout.

## Where things live

| Path | What |
|---|---|
| `app/main.py` | FastAPI app, router wiring, MasterList auto-seed |
| `app/models.py` | SQLAlchemy models (Trade / Pyramid / Exit / MasterListItem / CapitalEvent / Setting) |
| `app/calculations.py` | Pure functions — P/L, R:R, stock move, position sizing |
| `app/dashboard.py` | Monthly / yearly rollups |
| `app/importer.py` | xlsx → DB (destructive) |
| `app/routers/` | One file per feature |
| `app/templates/` | Jinja2 views |
| `data/journal.db` | SQLite database (gitignored) |

## Data is your data

Everything is in `data/journal.db`. Back it up by copying that one file. Move it to Postgres someday by swapping the SQLAlchemy URL in `app/db.py` — no other code changes.
