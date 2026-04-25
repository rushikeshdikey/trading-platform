"""Admin-only routes — user management.

All endpoints require `is_admin=True`. There's no public signup form;
admins create users with a temp password that's displayed once on the
create-success page (admin shares it out-of-band, e.g. via Signal/Slack).

To list users without the auto-filter clobbering the SELECT (which would
make admin only see themselves on per-user models — though `User` itself
isn't auto-filtered since it has no `user_id` of its own), queries that
need cross-user views use `.execution_options(skip_user_filter=True)`.
"""
from __future__ import annotations

import secrets
import string

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth as auth_mod
from .. import masterlist as masterlist_svc
from ..db import get_db
from ..deps import templates
from ..models import User

router = APIRouter(prefix="/admin")


def _gen_temp_password(length: int = 14) -> str:
    """Cryptographically random, mix of upper/lower/digit. No symbols so it
    survives copy-paste through chat clients without escaping issues."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.get("/users", response_class=HTMLResponse)
def list_users(
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(auth_mod.require_admin),
):
    users = (
        db.query(User)
        .order_by(User.created_at.asc())
        .all()
    )
    return templates.TemplateResponse(
        request, "admin/users.html",
        {
            "users": users,
            "ok": request.query_params.get("ok"),
            "err": request.query_params.get("err"),
        },
    )


@router.post("/users", response_class=HTMLResponse)
def create_user(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(""),
    is_admin: str = Form(""),  # "1" if checked
    db: Session = Depends(get_db),
    _admin: User = Depends(auth_mod.require_admin),
):
    email = email.strip().lower()
    if not email or "@" not in email:
        return RedirectResponse(url="/admin/users?err=invalid_email", status_code=303)
    existing = db.query(User).filter(User.email == email).first()
    if existing is not None:
        return RedirectResponse(url="/admin/users?err=email_taken", status_code=303)

    temp_password = _gen_temp_password()
    new_user = User(
        email=email,
        password_hash=auth_mod.hash_password(temp_password),
        full_name=full_name.strip() or None,
        is_admin=(is_admin == "1"),
        is_active=True,
        must_change_password=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    masterlist_svc.seed_for_user(db, new_user.id)

    # Surface temp password ONCE on a confirmation page. Reload the page
    # later → password is gone (forces admin to copy now).
    return templates.TemplateResponse(
        request, "admin/user_created.html",
        {"new_user": new_user, "temp_password": temp_password},
    )


@router.post("/users/{user_id}/reset-password", response_class=HTMLResponse)
def reset_password(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(auth_mod.require_admin),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    new_password = _gen_temp_password()
    target.password_hash = auth_mod.hash_password(new_password)
    target.must_change_password = True
    db.commit()
    return templates.TemplateResponse(
        request, "admin/user_created.html",
        {"new_user": target, "temp_password": new_password, "is_reset": True},
    )


@router.post("/users/{user_id}/toggle-active")
def toggle_active(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(auth_mod.require_admin),
):
    if user_id == admin.id:
        return RedirectResponse(url="/admin/users?err=cannot_deactivate_self", status_code=303)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    target.is_active = not target.is_active
    db.commit()
    return RedirectResponse(url="/admin/users?ok=updated", status_code=303)


@router.post("/users/{user_id}/toggle-admin")
def toggle_admin(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(auth_mod.require_admin),
):
    if user_id == admin.id:
        return RedirectResponse(url="/admin/users?err=cannot_demote_self", status_code=303)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404)
    target.is_admin = not target.is_admin
    db.commit()
    return RedirectResponse(url="/admin/users?ok=updated", status_code=303)
