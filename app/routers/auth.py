"""Zerodha Kite Connect login flow — per user.

Each user supplies their own Kite developer-app credentials (api_key +
secret) in /account before logging in. The endpoints here assume those are
already set; if not, they redirect the user to /account with an error.

Endpoints:
- GET  /auth/zerodha/login     → 303 to Kite login page
- GET  /auth/zerodha/callback  → exchange request_token, persist on User
- POST /auth/zerodha/logout    → clear the user's stored access_token
- POST /auth/zerodha/sync-instruments → refresh shared instrument-master cache
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth as user_auth
from .. import kite
from ..db import get_db
from ..models import User

router = APIRouter(prefix="/auth/zerodha")


@router.get("/login")
def login(request: Request, user: User = Depends(user_auth.require_user)):
    if not kite.is_configured(user):
        return RedirectResponse(
            url="/account?kite_error=not_configured", status_code=303,
        )
    return RedirectResponse(url=kite.login_url(user), status_code=303)


@router.get("/callback")
def callback(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(user_auth.require_user),
):
    request_token = request.query_params.get("request_token")
    status_param = request.query_params.get("status")
    if status_param == "error" or not request_token:
        msg = request.query_params.get("message", "Kite login was cancelled or failed.")
        return RedirectResponse(url=f"/account?kite_error={msg}", status_code=303)
    try:
        data = kite.exchange_request_token(db, user, request_token)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/account?kite_error={type(exc).__name__}: {exc}",
            status_code=303,
        )
    # Kick off the shared instrument-master sync (~10k rows, takes seconds).
    try:
        kite.sync_instruments(db, user)
        synced = "1"
    except Exception as exc:  # noqa: BLE001
        synced = f"0&kite_sync_error={type(exc).__name__}: {exc}"
    return RedirectResponse(
        url=f"/account?kite_connected=1&user={data.get('user_id','')}&synced={synced}",
        status_code=303,
    )


@router.post("/logout")
def logout(
    db: Session = Depends(get_db),
    user: User = Depends(user_auth.require_user),
):
    kite.logout(db, user)
    return RedirectResponse(url="/account", status_code=303)


@router.post("/sync-instruments")
def sync_instruments(
    db: Session = Depends(get_db),
    user: User = Depends(user_auth.require_user),
):
    try:
        result = kite.sync_instruments(db, user)
        return RedirectResponse(
            url=f"/account?kite_synced={result['total']}", status_code=303,
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/account?kite_sync_error={type(exc).__name__}: {exc}",
            status_code=303,
        )
