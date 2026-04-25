"""Authentication primitives: password hashing, session helpers, deps.

Auth model:
- Email + password only (no OAuth, no OTP — niche-user product).
- Admin (Rushikesh) creates accounts manually; users log in with what was
  shared and change their password on first login.
- Sessions are signed cookies (Starlette `SessionMiddleware`). The cookie
  carries `{"user_id": <int>}` and is HMAC'd with `SECRET_KEY`. No DB
  session table needed at this scale.
- Argon2id for password hashing — modern, slow on purpose, side-channel safe.

The `current_user` dependency is the single chokepoint for "is this request
allowed?" Routers call `Depends(require_user)` for any page that needs
auth and `Depends(require_admin)` for admin-only pages.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

_hasher = PasswordHasher()


# -- Password hashing --------------------------------------------------------


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHash):
        return False


def needs_rehash(hashed: str) -> bool:
    """Argon2 parameters can drift over time; rehash on login if so."""
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHash:
        return True


# -- Encryption (for per-user Kite credentials in Phase 3) -------------------
# Key is derived deterministically from SECRET_KEY so we don't need a separate
# secret. Anyone with SECRET_KEY can decrypt the Kite blobs — that's
# intentional: a server compromise that exposes SECRET_KEY also exposes the
# session cookies anyway, so it's not a meaningful additional attack surface.


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_str(value: str) -> bytes:
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt_str(blob: bytes) -> str:
    return _fernet().decrypt(blob).decode("utf-8")


# -- Session helpers ---------------------------------------------------------


def login_user(request: Request, user: User) -> None:
    request.session["user_id"] = user.id
    request.session["logged_in_at"] = datetime.utcnow().isoformat()


def logout_user(request: Request) -> None:
    request.session.clear()


def session_user_id(request: Request) -> int | None:
    return request.session.get("user_id")


# -- Dependencies ------------------------------------------------------------


def current_user(
    request: Request, db: Session = Depends(get_db)
) -> Optional[User]:
    """Returns the active User from the session cookie, or None.

    Doesn't raise — use `require_user` when you want a hard gate.
    """
    uid = session_user_id(request)
    if uid is None:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        # Stale cookie (account deactivated or deleted). Clear it so the
        # browser stops trying.
        request.session.clear()
        return None
    return user


class _RedirectToLogin(HTTPException):
    """Sentinel exception consumed by the auth middleware to issue a 303 to /login."""

    def __init__(self, next_url: str = "/"):
        super().__init__(status_code=status.HTTP_307_TEMPORARY_REDIRECT, detail=next_url)


def require_user(
    request: Request,
    user: Optional[User] = Depends(current_user),
) -> User:
    if user is None:
        # Preserve the originally-requested URL so we can come back after login.
        next_url = request.url.path or "/"
        if request.url.query:
            next_url += "?" + request.url.query
        raise _RedirectToLogin(next_url=next_url)
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only.")
    return user


# -- App-wide redirect handler for _RedirectToLogin --------------------------
# Registered in main.py — converts the sentinel into a 303 redirect that
# preserves the original URL via ?next=…


def login_redirect_response(next_url: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(
        url=f"/login?next={quote(next_url)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
