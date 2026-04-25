"""User-facing auth routes: login, logout, change-password, first-run setup.

Admin user management lives in `routers/admin.py` (added in M4).

Conventions:
- All form posts redirect via 303 (Post/Redirect/Get).
- Login errors and field errors are surfaced via flash-style query params
  (?err=...) — the templates render them as banner messages. Keeps the
  router handlers tiny.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth as auth_mod
from ..db import get_db
from ..deps import templates
from ..models import User

router = APIRouter()


# -- First-run admin setup ---------------------------------------------------
# If the users table is empty, the first visitor sees /setup and creates the
# admin account. After that, /setup 404s.


def _users_empty(db: Session) -> bool:
    return db.query(User).count() == 0


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, db: Session = Depends(get_db)):
    if not _users_empty(db):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request, "auth/setup.html",
        {"err": request.query_params.get("err")},
    )


@router.post("/setup")
def setup_submit(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(""),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if not _users_empty(db):
        return RedirectResponse(url="/login", status_code=303)
    email = email.strip().lower()
    if not email or "@" not in email:
        return RedirectResponse(url="/setup?err=invalid_email", status_code=303)
    if len(password) < 10:
        return RedirectResponse(url="/setup?err=weak_password", status_code=303)
    if password != password_confirm:
        return RedirectResponse(url="/setup?err=password_mismatch", status_code=303)

    admin = User(
        email=email,
        password_hash=auth_mod.hash_password(password),
        full_name=full_name.strip() or None,
        is_admin=True,
        is_active=True,
        must_change_password=False,  # they just set it
    )
    db.add(admin)
    db.commit()
    auth_mod.login_user(request, admin)
    return RedirectResponse(url="/", status_code=303)


# -- Login / logout ----------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    db: Session = Depends(get_db),
):
    # First-run shortcut: zero users → /setup.
    if _users_empty(db):
        return RedirectResponse(url="/setup", status_code=303)
    # Already logged in? Skip straight to next/home.
    if auth_mod.session_user_id(request) is not None:
        if auth_mod.current_user(request, db) is not None:
            return RedirectResponse(
                url=request.query_params.get("next") or "/", status_code=303,
            )
    return templates.TemplateResponse(
        request, "auth/login.html",
        {
            "next": request.query_params.get("next", ""),
            "err": request.query_params.get("err"),
            "email_prefill": request.query_params.get("email", ""),
        },
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if user is None or not user.is_active or not auth_mod.verify_password(
        password, user.password_hash
    ):
        # Single error message for both unknown email and bad password —
        # don't leak which one was wrong.
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/login?err=bad_creds&email={quote(email)}", status_code=303,
        )

    if auth_mod.needs_rehash(user.password_hash):
        user.password_hash = auth_mod.hash_password(password)

    user.last_login_at = datetime.utcnow()
    db.commit()
    auth_mod.login_user(request, user)

    if user.must_change_password:
        return RedirectResponse(url="/account/password", status_code=303)

    target = next or "/"
    if not target.startswith("/"):  # don't open redirect
        target = "/"
    return RedirectResponse(url=target, status_code=303)


@router.post("/logout")
def logout(request: Request):
    auth_mod.logout_user(request)
    return RedirectResponse(url="/login", status_code=303)


# -- Change password (any logged-in user) -----------------------------------


@router.get("/account/password", response_class=HTMLResponse)
def change_password_page(
    request: Request,
    user: User = Depends(auth_mod.require_user),
):
    return templates.TemplateResponse(
        request, "auth/change_password.html",
        {
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
            "user": user,
        },
    )


@router.post("/account/password")
def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    user: User = Depends(auth_mod.require_user),
    db: Session = Depends(get_db),
):
    if not auth_mod.verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/account/password?err=bad_current", status_code=303)
    if len(new_password) < 10:
        return RedirectResponse(url="/account/password?err=weak_password", status_code=303)
    if new_password != new_password_confirm:
        return RedirectResponse(url="/account/password?err=password_mismatch", status_code=303)

    user.password_hash = auth_mod.hash_password(new_password)
    user.must_change_password = False
    db.commit()
    return RedirectResponse(url="/account/password?ok=1", status_code=303)
