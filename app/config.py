"""App-wide configuration loaded from env vars.

`.env` at the project root is gitignored; copy `.env.example` and fill in
your values. Never commit secrets. The Kite Connect secret alone can't place
orders without the daily `access_token`, but anyone with both can read your
trades — treat it like a password.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

KITE_API_KEY: str | None = os.getenv("KITE_API_KEY")
KITE_API_SECRET: str | None = os.getenv("KITE_API_SECRET")
KITE_REDIRECT_URL: str = os.getenv(
    "KITE_REDIRECT_URL", "http://127.0.0.1:8000/auth/zerodha/callback"
)


def kite_configured() -> bool:
    return bool(KITE_API_KEY and KITE_API_SECRET)
