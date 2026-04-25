from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import jobs as jobs_mod
from . import prices
from . import orm_events  # noqa: F401  -- side-effect: registers SQLAlchemy events
from .auth import _RedirectToLogin, login_redirect_response, require_user
from .config import settings
from .db import SessionLocal
from .migrations import upgrade_to_head

settings.validate_for_runtime()
upgrade_to_head()

from .routers import (  # noqa: E402
    admin as admin_router,
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


@app.exception_handler(_RedirectToLogin)
async def _redirect_to_login(_request: Request, exc: _RedirectToLogin):
    return login_redirect_response(exc.detail)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Render a trader-themed 404 for missing routes / resources. Delegates
    every other status to Starlette's default plain-text response."""
    if exc.status_code == 404:
        from .deps import templates
        return templates.TemplateResponse(
            request, "404.html",
            {"path": request.url.path},
            status_code=404,
        )
    from starlette.responses import PlainTextResponse
    return PlainTextResponse(exc.detail or "Error", status_code=exc.status_code)


# Order matters: middlewares run in REVERSE order of registration. The LAST
# `add_middleware` call ends up OUTERMOST and runs first on each request.
# We want SessionMiddleware to run first (so .session is populated) and
# `_attach_user_to_request` to run inside it. So we register the user-
# attaching middleware FIRST, then SessionMiddleware second.

@app.middleware("http")
async def _attach_user_to_request(request: Request, call_next):
    """Make the current user available to templates AND to SQLAlchemy events.

    Runs INSIDE SessionMiddleware (so .session is decoded). Looks up the
    user, caches on request.state.user, and sets the request-scoped
    contextvar that `orm_events.py` reads to auto-filter per-user queries.

    Why set the contextvar HERE and not in `require_user`: FastAPI runs
    sync deps in an anyio worker thread, and ContextVar.set() inside a
    worker doesn't propagate back to the route handler's context. The
    middleware runs in the request's async task, so a contextvar set
    here is visible everywhere downstream — both in deps and in route
    handlers' threadpools (anyio copies the parent context into workers).
    """
    request.state.user = None
    try:
        uid = request.session.get("user_id")
    except AssertionError:
        uid = None
    if uid is not None:
        from .auth import current_user_id_var
        from .models import User
        with SessionLocal() as db:
            u = (
                db.query(User)
                .execution_options(skip_user_filter=True)
                .filter(User.id == uid)
                .first()
            )
            if u is not None and u.is_active:
                request.state.user = u
                current_user_id_var.set(u.id)
    return await call_next(request)


# Signed-cookie sessions. The cookie is HMAC'd with SECRET_KEY; rotating the
# key invalidates every existing session (intentional). Registered LAST so
# it ends up OUTERMOST and decodes the cookie before _attach_user_to_request
# runs.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie_name,
    max_age=settings.session_max_age_seconds,
    same_site="lax",
    https_only=settings.is_prod,
)


static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# Masterlist defaults are now seeded per-user (in /setup and admin user
# creation) instead of at boot — there's no global "current user" at boot.
prices.start_background_refresher()
jobs_mod.start()  # APScheduler — daily EOD pre-warm at 15:35 IST


@app.get("/api/status")
def api_status(user=Depends(require_user)):
    """Lightweight status summary consumed by the global nav strip."""
    from . import dashboard as _dash, kite as _kite

    with SessionLocal() as db:
        capital = _dash.current_capital(db)
        kite_status = _kite.auth_status(user)
        last = prices.last_refresh_at(db)
        from .models import Trade
        open_count = db.query(Trade).filter(Trade.status == "open").count()
    return {
        "capital": round(capital, 2),
        "kite_authed": kite_status["authed"],
        "kite_configured": kite_status["configured"],
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
app.include_router(admin_router.router, dependencies=_authed)
