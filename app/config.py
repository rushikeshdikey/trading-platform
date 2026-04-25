"""App-wide configuration loaded from env vars.

`.env` at the project root is gitignored; copy `.env.example` and fill in
your values. Never commit secrets. The Kite Connect secret alone can't place
orders without the daily `access_token`, but anyone with both can read your
trades — treat it like a password.

Two surfaces:
- `settings` — pydantic-settings object, source of truth for new code.
- Module-level constants (`KITE_API_KEY`, `kite_configured()`, etc.) — kept
  for backwards compatibility with existing callers (kite.py, routers/auth).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SQLITE_URL = f"sqlite:///{_PROJECT_ROOT / 'data' / 'journal.db'}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: str = Field(default="dev")
    secret_key: str = Field(default="dev-insecure-change-me-in-production-abcdefghijk")
    database_url: str = Field(default=_DEFAULT_SQLITE_URL)

    kite_api_key: str | None = None
    kite_api_secret: str | None = None
    kite_redirect_url: str = Field(
        default="http://127.0.0.1:8000/auth/zerodha/callback"
    )

    session_cookie_name: str = "tj_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 14

    @property
    def is_prod(self) -> bool:
        return self.env.lower() in ("prod", "production")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def validate_for_runtime(self) -> None:
        """Refuse to boot if production config is unsafe.

        Dev mode is permissive so the local journey stays one-command.
        """
        if not self.is_prod:
            return
        if self.secret_key.startswith("dev-insecure") or len(self.secret_key) < 32:
            raise RuntimeError(
                "SECRET_KEY must be set to a strong (>=32 char) value in production."
            )
        if self.is_sqlite:
            raise RuntimeError(
                "SQLite is not allowed in production. Set DATABASE_URL to a Postgres URL."
            )


settings = Settings()


KITE_API_KEY: str | None = settings.kite_api_key
KITE_API_SECRET: str | None = settings.kite_api_secret
KITE_REDIRECT_URL: str = settings.kite_redirect_url


def kite_configured() -> bool:
    return bool(settings.kite_api_key and settings.kite_api_secret)
