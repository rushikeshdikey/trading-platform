from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import masterlist, prices
from .auth import _RedirectToLogin, login_redirect_response, require_user
from .config import settings
from .db import SessionLocal
from .migrations import upgrade_to_head

settings.validate_for_runtime()
upgrade_to_head()

from .routers import (  # noqa: E402
    auth as kite_auth,
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
    users as users_router,
    watchlist,
)

app = FastAPI(title="Trading Journal")

# Signed-cookie sessions. The cookie is HMAC'd with SECRET_KEY; rotating the
# key invalidates every existing session (intentional).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie_name,
    max_age=settings.session_max_age_seconds,
    same_site="lax",
    https_only=settings.is_prod,
)


@app.exception_handler(_RedirectToLogin)
async def _redirect_to_login(_request: Request, exc: _RedirectToLogin):
    return login_redirect_response(exc.detail)


static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


with SessionLocal() as db:
    masterlist.seed_defaults(db)

prices.start_background_refresher()


@app.get("/api/status", dependencies=[Depends(require_user)])
def api_status():
    """Lightweight status summary consumed by the global nav strip."""
    from . import dashboard as _dash, kite as _kite

    with SessionLocal() as db:
        capital = _dash.current_capital(db)
        kite_status = _kite.auth_status(db)
        last = prices.last_refresh_at(db)
        from .models import Trade
        open_count = db.query(Trade).filter(Trade.status == "open").count()
    return {
        "capital": round(capital, 2),
        "kite_authed": kite_status["authed"],
        "kite_user": kite_status.get("user_name") or kite_status.get("user_id"),
        "open_positions": open_count,
        "prices_last_refresh": last.isoformat() if last else None,
    }


@app.get("/")
def root():
    return RedirectResponse(url="/cockpit", status_code=303)


@app.get("/health")
def health():
    """Public — used by uptime checks."""
    return {"ok": True, "today": date.today().isoformat()}


# Public auth routes (login/setup/logout/change-password). These define their
# own dependencies internally where needed.
app.include_router(users_router.router)


# Every other router gets gated globally. Adding `require_user` here means
# we don't have to touch each router file — a single source of truth for
# "do you need to be logged in to see this?".
_authed = [Depends(require_user)]

app.include_router(cockpit_router.router, dependencies=_authed)
app.include_router(dashboard.router, dependencies=_authed)
app.include_router(trades.router, dependencies=_authed)
app.include_router(sizing.router, dependencies=_authed)
app.include_router(masterlist_routes.router, dependencies=_authed)
app.include_router(settings_routes.router, dependencies=_authed)
app.include_router(imports.router, dependencies=_authed)
app.include_router(prices_router.router, dependencies=_authed)
app.include_router(kite_auth.router, dependencies=_authed)
app.include_router(instruments.router, dependencies=_authed)
app.include_router(insights.router, dependencies=_authed)
app.include_router(breadth_router.router, dependencies=_authed)
app.include_router(scanners.router, dependencies=_authed)
app.include_router(watchlist.router, dependencies=_authed)
