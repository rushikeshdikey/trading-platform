from datetime import date
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import masterlist, prices
from .db import Base, SessionLocal, engine
from .routers import (
    auth,
    breadth as breadth_router,
    cockpit as cockpit_router,
    dashboard,
    imports,
    instruments,
    insights,
    masterlist_routes,
    prices as prices_router,
    scanners,
    settings_routes,
    sizing,
    trades,
    watchlist,
)

Base.metadata.create_all(bind=engine)

# Additive schema migrations for columns added after initial DB creation.
from .db import apply_schema_additions  # noqa: E402

_applied = apply_schema_additions()
if _applied:
    import logging

    logging.getLogger("journal").info("DB schema additions applied: %s", _applied)

app = FastAPI(title="Trading Journal")

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


with SessionLocal() as db:
    masterlist.seed_defaults(db)

prices.start_background_refresher()


@app.get("/api/status")
def api_status():
    """Lightweight status summary consumed by the global nav strip."""
    from . import dashboard as _dash, kite as _kite

    with SessionLocal() as db:
        capital = _dash.current_capital(db)
        auth = _kite.auth_status(db)
        last = prices.last_refresh_at(db)
        from .models import Trade
        open_count = db.query(Trade).filter(Trade.status == "open").count()
    return {
        "capital": round(capital, 2),
        "kite_authed": auth["authed"],
        "kite_user": auth.get("user_name") or auth.get("user_id"),
        "open_positions": open_count,
        "prices_last_refresh": last.isoformat() if last else None,
    }


@app.get("/")
def root():
    return RedirectResponse(url="/cockpit", status_code=303)


@app.get("/health")
def health():
    return {"ok": True, "today": date.today().isoformat()}


app.include_router(cockpit_router.router)
app.include_router(dashboard.router)
app.include_router(trades.router)
app.include_router(sizing.router)
app.include_router(masterlist_routes.router)
app.include_router(settings_routes.router)
app.include_router(imports.router)
app.include_router(prices_router.router)
app.include_router(auth.router)
app.include_router(instruments.router)
app.include_router(insights.router)
app.include_router(breadth_router.router)
app.include_router(scanners.router)
app.include_router(watchlist.router)
