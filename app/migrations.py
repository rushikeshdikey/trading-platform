"""Run Alembic migrations programmatically at app boot.

Container deploys typically run `alembic upgrade head` as a separate step;
calling it from app boot is fine for the small-scale single-VM target where
we don't have an init container.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import settings

log = logging.getLogger("journal.migrations")


def upgrade_to_head() -> None:
    cfg_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(cfg_path))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    log.info("alembic upgrade head (db=%s)", _redact(settings.database_url))
    command.upgrade(cfg, "head")


def _redact(url: str) -> str:
    # postgresql+psycopg://user:pass@host/db -> postgresql+psycopg://user:***@host/db
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return url
