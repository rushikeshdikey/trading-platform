"""Helpers for MasterList dropdown categories."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import MasterListItem

CATEGORIES = ("setup", "proficiency", "growth_area", "exit_trigger", "base_duration")

CATEGORY_LABELS = {
    "setup": "Setups",
    "proficiency": "Proficiency tags",
    "growth_area": "Growth areas",
    "exit_trigger": "Exit triggers",
    "base_duration": "Base durations",
}


# Seeded from the xlsx MasterList + observed values in DTrades.
DEFAULTS = {
    "setup": [
        "Base on Base", "OSHL", "IPO base", "Low Cheat", "Shakeout",
        "Flag", "Trendline", "No Setup",
    ],
    "proficiency": [
        "Emotional Control", "Exited in Strength", "Good Entry", "Good Trailing",
        "Protected Breakeven", "Small SL", "Well Managed",
    ],
    "growth_area": [
        "Biased Analysis", "Booked Early", "Didn't Book Loss", "Too Tight SL",
        "FOMO", "Illiquid Stock", "Illogical SL", "Lack of Patience",
    ],
    "exit_trigger": [
        "Breakeven exit", "Market Pressure", "R multiples", "Random",
        "Rejection", "Setup Failed", "SL", "Emotional",
    ],
    "base_duration": [
        "1 W", "2 W", "1 M", "2-3 M", "3-4 M", "5-6 M", "6-12 M",
    ],
}


def values(db: Session, category: str) -> list[str]:
    rows = (
        db.query(MasterListItem)
        .filter(MasterListItem.category == category)
        .order_by(MasterListItem.sort_order, MasterListItem.value)
        .all()
    )
    return [r.value for r in rows]


def all_dropdowns(db: Session) -> dict[str, list[str]]:
    return {c: values(db, c) for c in CATEGORIES}


def seed_defaults(db: Session) -> None:
    existing = {(r.category, r.value) for r in db.query(MasterListItem).all()}
    for cat, vals in DEFAULTS.items():
        for i, v in enumerate(vals):
            if (cat, v) not in existing:
                db.add(MasterListItem(category=cat, value=v, sort_order=i))
    db.commit()


def add_value(db: Session, category: str, value: str) -> None:
    if category not in CATEGORIES:
        return
    value = value.strip()
    if not value:
        return
    exists = (
        db.query(MasterListItem)
        .filter(MasterListItem.category == category, MasterListItem.value == value)
        .first()
    )
    if exists:
        return
    max_order = (
        db.query(MasterListItem.sort_order)
        .filter(MasterListItem.category == category)
        .order_by(MasterListItem.sort_order.desc())
        .first()
    )
    next_order = (max_order[0] + 1) if max_order else 0
    db.add(MasterListItem(category=category, value=value, sort_order=next_order))
    db.commit()


def delete_value(db: Session, item_id: int) -> None:
    row = db.get(MasterListItem, item_id)
    if row:
        db.delete(row)
        db.commit()
