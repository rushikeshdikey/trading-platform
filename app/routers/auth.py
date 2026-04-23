"""Zerodha Kite Connect login flow.

Three endpoints:
- GET  /auth/zerodha/login     → 303 redirect to Kite login
- GET  /auth/zerodha/callback  → Kite redirects here with `request_token`; we
                                 exchange it, store the access token, and
                                 redirect to /settings.
- POST /auth/zerodha/logout    → clear the stored token.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import config, kite
from ..db import get_db

router = APIRouter(prefix="/auth/zerodha")


@router.get("/login")
def login(request: Request):
    if not config.kite_configured():
        return HTMLResponse(
            "<h1>Kite is not configured</h1>"
            "<p>Set KITE_API_KEY and KITE_API_SECRET in the .env file "
            "at the project root, then restart the app.</p>",
            status_code=500,
        )
    return RedirectResponse(url=kite.login_url(), status_code=303)


@router.get("/callback")
def callback(request: Request, db: Session = Depends(get_db)):
    request_token = request.query_params.get("request_token")
    status_param = request.query_params.get("status")
    if status_param == "error" or not request_token:
        msg = request.query_params.get("message", "Kite login was cancelled or failed.")
        return RedirectResponse(
            url=f"/settings?kite_error={msg}", status_code=303
        )
    try:
        data = kite.exchange_request_token(db, request_token)
    except Exception as exc:  # noqa: BLE001 — surface the error to the user
        return RedirectResponse(
            url=f"/settings?kite_error={type(exc).__name__}: {exc}", status_code=303
        )
    # Kick off an instrument-master sync in the same request — it's ~10k rows,
    # takes a couple of seconds, and every other feature depends on it.
    try:
        kite.sync_instruments(db)
        synced = "1"
    except Exception as exc:  # noqa: BLE001
        synced = f"0&kite_sync_error={type(exc).__name__}: {exc}"
    return RedirectResponse(
        url=f"/settings?kite_connected=1&user={data.get('user_id','')}&synced={synced}",
        status_code=303,
    )


@router.post("/logout")
def logout(db: Session = Depends(get_db)):
    kite.logout(db)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/sync-instruments")
def sync_instruments(db: Session = Depends(get_db)):
    try:
        result = kite.sync_instruments(db)
        return RedirectResponse(
            url=f"/settings?kite_synced={result['total']}", status_code=303
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/settings?kite_sync_error={type(exc).__name__}: {exc}",
            status_code=303,
        )
