from sqlalchemy.orm import Session

from .models import Setting

DEFAULTS = {
    "starting_capital": "1000000",
    "starting_capital_date": "2025-01-01",
    "default_risk_pct": "0.005",  # 0.5% of capital — stored as fraction
    "default_allocation_pct": "0.10",  # 10% — stored as fraction
    "default_sl_pct": "0.025",  # 2.5% SL distance — stored as fraction
    "currency_symbol": "₹",
}


def get(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(Setting, key)
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
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def all_settings(db: Session) -> dict[str, str]:
    rows = db.query(Setting).all()
    merged = dict(DEFAULTS)
    for r in rows:
        merged[r.key] = r.value
    return merged
