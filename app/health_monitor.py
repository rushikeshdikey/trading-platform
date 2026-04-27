"""Periodic in-process health probe + status-page query helpers.

Every 60 seconds the scheduler calls ``probe_and_log`` which times an
internal call to the /health logic and writes a HealthCheck row. The
public /status page reads the last 24h and renders a timeline.

When the app is down, the scheduler can't write — but the GAP in the
timeline (missing rows for that minute) is itself the downtime signal.

Storage: rolling — older than KEEP_DAYS gets pruned every probe so the
table stays small (60s × 60min × 24h × 7d = ~10k rows max).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import HealthCheck

log = logging.getLogger("journal.health")

KEEP_DAYS = 7
PROBE_INTERVAL_S = 60


def probe_once() -> tuple[bool, int, str | None]:
    """Internal-only health probe — exercises the same path /health does
    but in-process (no HTTP overhead). Returns (ok, response_ms, error)."""
    started = time.perf_counter()
    try:
        with SessionLocal() as db:
            db.execute(func.count(HealthCheck.id).select())
        ok = True
        err = None
    except Exception as exc:  # noqa: BLE001
        ok = False
        err = f"{type(exc).__name__}: {exc}"[:200]
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return ok, elapsed_ms, err


def probe_and_log() -> None:
    ok, ms, err = probe_once()
    try:
        with SessionLocal() as db:
            db.add(HealthCheck(
                checked_at=datetime.utcnow(),
                ok=ok, response_ms=ms, error=err,
            ))
            # Roll the table — keep only the last KEEP_DAYS days.
            cutoff = datetime.utcnow() - timedelta(days=KEEP_DAYS)
            db.query(HealthCheck).filter(HealthCheck.checked_at < cutoff).delete()
            db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("health_check write failed: %s", exc)


# ---------------------------------------------------------------------------
# Read-side: aggregation helpers for the /status template
# ---------------------------------------------------------------------------


@dataclass
class StatusSlot:
    """One bucket on the timeline — green (all probes ok), red (any
    probe failed), or grey (no data — app was likely down).

    ``pre_deploy=True`` means the bucket pre-dates the very first probe
    row in the database — i.e., the probe loop didn't EXIST yet, so the
    absence of data is not a downtime signal. Rendered with a distinct
    style so users don't misread "feature didn't ship yet" as "app was
    down for 6 days".
    """
    label: str            # "14:35" or "Apr 27"
    ok_count: int
    fail_count: int
    no_data: bool         # true if 0 probes
    avg_ms: int | None    # average response time, or None if no data
    pre_deploy: bool = False

    @property
    def color(self) -> str:
        if self.pre_deploy:
            return "pre-deploy"
        if self.no_data:
            return "no-data"
        if self.fail_count > 0:
            return "red" if self.fail_count >= max(1, self.ok_count) else "amber"
        return "green"


@dataclass
class StatusSummary:
    slots_24h: list[StatusSlot]      # 1 slot per 5 minutes  (288 slots)
    slots_7d: list[StatusSlot]       # 1 slot per day        (7 slots)
    uptime_24h_pct: float
    uptime_7d_pct: float
    latest_ok: bool
    latest_at: datetime | None
    latest_response_ms: int | None
    total_probes_24h: int
    total_failures_24h: int


def _bucket_rows(
    rows: Iterable[HealthCheck],
    buckets: list[datetime],
    slot_seconds: int,
    first_probe_at: datetime | None = None,
) -> list[StatusSlot]:
    """Slot HealthCheck rows into time buckets. ``buckets`` is the list
    of bucket-START datetimes; rows are placed by their checked_at.

    Buckets whose END is before ``first_probe_at`` are stamped with
    ``pre_deploy=True`` so the UI can distinguish "probe loop didn't
    exist yet" from "app was down".
    """
    by_bucket: dict[int, list[HealthCheck]] = defaultdict(list)
    bucket_starts = [b.timestamp() for b in buckets]
    for r in rows:
        ts = r.checked_at.timestamp()
        # Binary-search would be O(log n); linear is fine at our row counts.
        idx = -1
        for i, start in enumerate(bucket_starts):
            if start <= ts < start + slot_seconds:
                idx = i
                break
        if idx >= 0:
            by_bucket[idx].append(r)

    out: list[StatusSlot] = []
    for i, start in enumerate(buckets):
        chunk = by_bucket.get(i, [])
        ok = sum(1 for r in chunk if r.ok)
        fail = sum(1 for r in chunk if not r.ok)
        no_data = len(chunk) == 0
        avg_ms = int(sum(r.response_ms or 0 for r in chunk) / len(chunk)) if chunk else None
        bucket_end = start + timedelta(seconds=slot_seconds)
        pre_deploy = (
            no_data
            and first_probe_at is not None
            and bucket_end <= first_probe_at
        )
        # Label: HH:MM for ≤ 1h slots, "MMM DD" for daily slots.
        if slot_seconds < 3600:
            label = start.strftime("%H:%M")
        else:
            label = start.strftime("%b %d")
        out.append(StatusSlot(
            label=label, ok_count=ok, fail_count=fail,
            no_data=no_data, avg_ms=avg_ms, pre_deploy=pre_deploy,
        ))
    return out


def build_summary(db: Session) -> StatusSummary:
    """Build the StatusSummary from the last 7 days of HealthCheck rows."""
    now = datetime.utcnow()
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    rows_24h = (
        db.query(HealthCheck)
        .filter(HealthCheck.checked_at >= cutoff_24h)
        .order_by(HealthCheck.checked_at.asc())
        .all()
    )
    rows_7d = (
        db.query(HealthCheck)
        .filter(HealthCheck.checked_at >= cutoff_7d)
        .order_by(HealthCheck.checked_at.asc())
        .all()
    )

    # Earliest probe row IN THE WHOLE TABLE — used to mark buckets that
    # pre-date the probe loop existing as "no service history yet"
    # rather than "app was down". Without this, a brand-new prod looks
    # like 6 days of downtime in the 7-day chart.
    first_probe_at = (
        db.query(func.min(HealthCheck.checked_at)).scalar()
    )

    # 24h timeline: 5-minute buckets → 288 slots
    slot_24h_seconds = 5 * 60
    n_24h = (24 * 3600) // slot_24h_seconds
    buckets_24h = [
        cutoff_24h + timedelta(seconds=i * slot_24h_seconds) for i in range(n_24h)
    ]
    slots_24h = _bucket_rows(rows_24h, buckets_24h, slot_24h_seconds, first_probe_at)

    # 7d timeline: 1-day buckets → 7 slots
    slot_7d_seconds = 24 * 3600
    buckets_7d = [
        (cutoff_7d + timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(7)
    ]
    slots_7d = _bucket_rows(rows_7d, buckets_7d, slot_7d_seconds, first_probe_at)

    n_24h_probes = len(rows_24h)
    n_24h_fails = sum(1 for r in rows_24h if not r.ok)
    n_7d_probes = len(rows_7d)
    n_7d_fails = sum(1 for r in rows_7d if not r.ok)

    # Uptime %: probes that succeeded ÷ EXPECTED probes (1/minute) — gaps
    # from "app was down, scheduler couldn't write" count against us.
    # Denominator is RAMPED — we use the time since the oldest probe in
    # the window, capped at the full window. First minute of probing
    # shows 100%; after 24h+ we use the full 1440 expected. Without this
    # ramp, a freshly-deployed instance would show 1% uptime instead of
    # the correct "all probes succeeded so far."
    def _expected_probes(rows: list[HealthCheck], full_window_s: int) -> int:
        if not rows:
            return 1  # avoid division by zero; result will be 0/1 = 0%
        elapsed_s = (now - rows[0].checked_at).total_seconds()
        elapsed_s = max(PROBE_INTERVAL_S, min(elapsed_s, full_window_s))
        return max(1, int(elapsed_s // PROBE_INTERVAL_S))

    expected_24h = _expected_probes(rows_24h, 24 * 3600)
    expected_7d = _expected_probes(rows_7d, 7 * 24 * 3600)
    ok_24h = n_24h_probes - n_24h_fails
    ok_7d = n_7d_probes - n_7d_fails
    uptime_24h_pct = (ok_24h / expected_24h * 100.0) if expected_24h > 0 else 0.0
    uptime_7d_pct = (ok_7d / expected_7d * 100.0) if expected_7d > 0 else 0.0

    latest = rows_24h[-1] if rows_24h else None
    return StatusSummary(
        slots_24h=slots_24h,
        slots_7d=slots_7d,
        uptime_24h_pct=round(min(uptime_24h_pct, 100.0), 2),
        uptime_7d_pct=round(min(uptime_7d_pct, 100.0), 2),
        latest_ok=bool(latest.ok) if latest else False,
        latest_at=latest.checked_at if latest else None,
        latest_response_ms=int(latest.response_ms) if latest else None,
        total_probes_24h=n_24h_probes,
        total_failures_24h=n_24h_fails,
    )
