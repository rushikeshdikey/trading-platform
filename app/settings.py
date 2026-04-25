"""Per-user key/value settings.

Composite PK is `(user_id, key)`. Lookups go through filtered queries — the
`do_orm_execute` event in `orm_events.py` adds the user filter automatically
based on the request-scoped contextvar, so callers don't pass user_id
explicitly. Outside a request (background tasks, tests) you'd need to set
the contextvar yourself or pass `user_id` via a future overload.
"""
from sqlalchemy.orm import Session

from .models import Setting

DEFAULTS = {
    "starting_capital": "1000000",
    "starting_capital_date": "2025-01-01",
    "default_risk_pct": "0.005",
    "default_allocation_pct": "0.10",
    "default_sl_pct": "0.025",
    "currency_symbol": "₹",
}


def get(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row is not None:
        return row.value
    return DEFAULTS.get(key, default)


def get_float(db: Session, key: str, default: float = 0.0) -> float:
    raw = get(db, key)
    try:
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def set_value(db: Session, key: str, value: str) -> None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row is None:
        # `before_flush` event stamps user_id from the contextvar.
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def all_settings(db: Session) -> dict[str, str]:
    rows = db.query(Setting).all()
    merged = dict(DEFAULTS)
    for r in rows:
        merged[r.key] = r.value
    return merged
