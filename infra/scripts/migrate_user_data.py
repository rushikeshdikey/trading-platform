"""Migrate per-user data between two databases.

Two modes:

  python migrate_user_data.py export --source-url sqlite:///data/journal.db
                                     --user-email rushikesh.dikey@zilionix.com
                                     --out user_data.json.gz

  python migrate_user_data.py import --target-url postgresql+psycopg://...
                                     --user-email rushikesh.dikey@zilionix.com
                                     --in user_data.json.gz

Per-user tables only (Trade, Pyramid, Exit, MasterListItem, Setting,
CapitalEvent, Watchlist, ScanRun, ImportedExecution). Shared market-data
tables are NOT migrated — those regenerate from bhavcopy / fundamentals.

Import behaviour:
- Maps every row's ``user_id`` to the prod admin's id (look up by email).
- Trade.id may differ between source and target (autoincrement) — we
  rebuild the Trade with a fresh id and remap its Pyramid/Exit children
  to the new id. ImportedExecution.applied_to_trade_id is also remapped.
- Settings + Watchlist get UPSERT-style merge so re-running is idempotent.
- Other tables INSERT and dedupe by (user_id, natural-key) where possible.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

# Make the app package importable regardless of cwd.
_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base, Trade, Pyramid, Exit, MasterListItem, Setting, CapitalEvent,
    Watchlist, ScanRun, ImportedExecution, User,
)


def _row_to_dict(row, exclude=()):
    return {
        c.name: getattr(row, c.name)
        for c in row.__table__.columns
        if c.name not in exclude
    }


def export(source_url: str, user_email: str, out_path: str) -> None:
    eng = create_engine(source_url, future=True)
    Session = sessionmaker(bind=eng, autoflush=False)
    db = Session()

    user = db.query(User).filter(User.email == user_email.lower()).first()
    if user is None:
        raise SystemExit(f"User {user_email!r} not found in source DB.")
    print(f"Source user: id={user.id} email={user.email}", flush=True)

    payload: dict[str, list] = {}

    # Per-user tables that own a `user_id` FK.
    for Model in [Trade, MasterListItem, CapitalEvent, Watchlist, ScanRun]:
        rows = db.query(Model).filter(Model.user_id == user.id).all()
        payload[Model.__tablename__] = [_row_to_dict(r) for r in rows]

    # Composite-PK per-user tables.
    rows = db.query(Setting).filter(Setting.user_id == user.id).all()
    payload["settings"] = [_row_to_dict(r) for r in rows]

    rows = db.query(ImportedExecution).filter(
        ImportedExecution.user_id == user.id,
    ).all()
    payload["imported_executions"] = [_row_to_dict(r) for r in rows]

    # Pyramid + Exit cascade through Trade.
    trade_ids = [t["id"] for t in payload["trades"]]
    pyrs = db.query(Pyramid).filter(Pyramid.trade_id.in_(trade_ids)).all() if trade_ids else []
    exits = db.query(Exit).filter(Exit.trade_id.in_(trade_ids)).all() if trade_ids else []
    payload["pyramids"] = [_row_to_dict(r) for r in pyrs]
    payload["exits"] = [_row_to_dict(r) for r in exits]

    # Sanity log.
    print("Row counts:", {k: len(v) for k, v in payload.items()}, flush=True)

    raw = json.dumps(payload, default=str).encode("utf-8")
    if out_path.endswith(".gz"):
        with gzip.open(out_path, "wb", compresslevel=9) as f:
            f.write(raw)
    else:
        Path(out_path).write_bytes(raw)
    print(f"Wrote {out_path} ({len(raw)} bytes raw)", flush=True)
    db.close()


def import_(target_url: str, user_email: str, in_path: str) -> None:
    eng = create_engine(target_url, future=True)
    Session = sessionmaker(bind=eng, autoflush=False)
    db = Session()

    target = db.query(User).filter(User.email == user_email.lower()).first()
    if target is None:
        raise SystemExit(
            f"User {user_email!r} not found in target. Create it via /setup first."
        )
    print(f"Target user: id={target.id} email={target.email}", flush=True)

    raw = Path(in_path).read_bytes()
    if in_path.endswith(".gz"):
        raw = gzip.decompress(raw)
    payload = json.loads(raw)
    print("Importing:", {k: len(v) for k, v in payload.items()}, flush=True)

    # ---- 1. Settings — composite PK (user_id, key); upsert by merge ----
    for r in payload.get("settings", []):
        existing = db.query(Setting).filter(
            Setting.user_id == target.id, Setting.key == r["key"],
        ).first()
        if existing is None:
            db.add(Setting(user_id=target.id, key=r["key"], value=r["value"]))
        else:
            existing.value = r["value"]

    # ---- 2. MasterListItem — dedupe on (user_id, category, value) ----
    seen = {
        (m.category, m.value)
        for m in db.query(MasterListItem).filter(MasterListItem.user_id == target.id).all()
    }
    for r in payload.get("masterlist_items", []):
        if (r["category"], r["value"]) in seen:
            continue
        d = {**r, "user_id": target.id}
        d.pop("id", None)
        db.add(MasterListItem(**d))

    # ---- 3. CapitalEvent — INSERT, no natural dedup key ----
    if payload.get("capital_events"):
        existing_count = db.query(CapitalEvent).filter(
            CapitalEvent.user_id == target.id,
        ).count()
        if existing_count == 0:
            for r in payload["capital_events"]:
                d = {**r, "user_id": target.id}
                d.pop("id", None)
                db.add(CapitalEvent(**d))
        else:
            print(f"  capital_events skipped — target has {existing_count} rows already")

    # ---- 4. Watchlist — composite uniq (user_id, symbol) ----
    seen_w = {
        w.symbol for w in db.query(Watchlist).filter(Watchlist.user_id == target.id).all()
    }
    for r in payload.get("watchlist", []):
        if r["symbol"] in seen_w:
            continue
        d = {**r, "user_id": target.id}
        d.pop("id", None)
        db.add(Watchlist(**d))

    # ---- 5. ScanRun — history; INSERT all, no dedup ----
    if not db.query(ScanRun).filter(ScanRun.user_id == target.id).count():
        for r in payload.get("scan_runs", []) + payload.get("scan_run", []):
            d = {**r, "user_id": target.id}
            d.pop("id", None)
            db.add(ScanRun(**d))

    # Flush so the FK targets exist when we add Pyramids/Exits.
    db.flush()

    # ---- 6. Trade + children — only if target has zero trades ----
    existing_trades = db.query(Trade).filter(Trade.user_id == target.id).count()
    if existing_trades > 0:
        print(f"  trades skipped — target has {existing_trades} already.")
        print("  (If you want to wipe and re-import, do that manually)")
    else:
        # Map old trade_id -> new Trade ORM instance so we can attach children.
        trade_map: dict[int, Trade] = {}
        for r in payload.get("trades", []):
            old_id = r["id"]
            d = {**r, "user_id": target.id}
            d.pop("id", None)
            t = Trade(**d)
            db.add(t)
            trade_map[old_id] = t

        db.flush()  # populate the new Trade.id values

        for r in payload.get("pyramids", []):
            d = {**r}
            old_trade_id = d.pop("trade_id")
            d.pop("id", None)
            new_trade = trade_map.get(old_trade_id)
            if new_trade is None:
                continue
            d["trade_id"] = new_trade.id
            db.add(Pyramid(**d))

        for r in payload.get("exits", []):
            d = {**r}
            old_trade_id = d.pop("trade_id")
            d.pop("id", None)
            new_trade = trade_map.get(old_trade_id)
            if new_trade is None:
                continue
            d["trade_id"] = new_trade.id
            db.add(Exit(**d))

        # ---- 7. ImportedExecution — composite PK (user_id, trade_id-from-broker)
        # and applied_to_trade_id needs remapping to the new Trade.id.
        for r in payload.get("imported_executions", []):
            d = {**r, "user_id": target.id}
            old_apply = d.get("applied_to_trade_id")
            if old_apply is not None:
                new_t = trade_map.get(old_apply)
                d["applied_to_trade_id"] = new_t.id if new_t else None
            db.add(ImportedExecution(**d))

    db.commit()
    print("Import complete.")
    db.close()


def _ts_to_dt(d):
    """JSON encoded datetimes/dates as ISO strings — SQLAlchemy will parse
    most back automatically when the column type is Date/DateTime, but
    Postgres is stricter, so we coerce here."""
    return d  # left as a hook — coercion handled by SQLAlchemy column types


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export")
    e.add_argument("--source-url", required=True)
    e.add_argument("--user-email", required=True)
    e.add_argument("--out", required=True)

    i = sub.add_parser("import")
    i.add_argument("--target-url", required=True)
    i.add_argument("--user-email", required=True)
    i.add_argument("--in", dest="in_", required=True)

    args = ap.parse_args()
    if args.cmd == "export":
        export(args.source_url, args.user_email, args.out)
    else:
        import_(args.target_url, args.user_email, args.in_)


if __name__ == "__main__":
    main()
