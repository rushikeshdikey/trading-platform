"""Background scheduler — APScheduler in-process.

Hosts the EOD pre-warm: every weekday at 15:35 IST (5 minutes after market
close), refresh the bhavcopy + run all 4 scanners + cache the results in
``ScanCache``. Page-loads at /scanners then read from cache in ~50ms.

Why in-process: at one VM scale we don't need Celery/RQ/Redis. APScheduler
runs in a daemon thread and survives across requests in the same uvicorn
worker. With gunicorn+UvicornWorker(2 workers), TWO schedulers would race
to write the same cache rows — fine because the upsert is idempotent and
the late writer just overwrites with identical payload, but to avoid double
work we gate via a leader-election advisory lock at job entry.

Hooks into ``app/main.py`` boot. Stop is automatic on process exit
(daemon=True). Tests can opt-out via ``DISABLE_SCHEDULER=1``.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import SessionLocal

log = logging.getLogger("journal.jobs")

IST = timezone(timedelta(hours=5, minutes=30))

_scheduler: Optional[BackgroundScheduler] = None
_leader_lock = threading.Lock()


def _try_acquire_leader(db) -> bool:
    """Coarse leader election — multi-worker setups would otherwise have N
    schedulers running the same job. We use the ``scan_cache`` table itself
    as a soft mutex: only run if no row was updated in the last 60 seconds.

    Race window is fine because the worst case is two workers each running
    the scan and double-writing the same rows. With gunicorn=2 workers this
    saves ~3s of duplicated CPU per pre-warm; not strictly necessary.
    """
    from .models import ScanCache
    recent = (
        db.query(ScanCache)
        .filter(ScanCache.run_at >= datetime.utcnow() - timedelta(seconds=60))
        .first()
    )
    return recent is None


def _eod_prewarm() -> None:
    """Refresh bars cache + run all scanners + cache payloads."""
    if not _leader_lock.acquire(blocking=False):
        log.info("eod_prewarm skipped: another invocation in progress")
        return
    try:
        with SessionLocal() as db:
            if not _try_acquire_leader(db):
                log.info("eod_prewarm skipped: another worker just ran it")
                return

            t0 = time.perf_counter()
            log.info("eod_prewarm: starting")

            # 1. Pull today's bhavcopy. Skip silently on weekends/holidays
            #    (bhavcopy returns 404 — bars_cache handles that).
            try:
                from .scanner import bars_cache as bc
                bars_summary = bc.refresh_bars(db, lookback_days=180)
                log.info(
                    "eod_prewarm bars: downloaded=%d skipped=%d failed=%d rows=%d",
                    bars_summary.days_downloaded,
                    bars_summary.days_skipped_existing,
                    bars_summary.days_failed,
                    bars_summary.rows_upserted,
                )
            except Exception:
                log.exception("eod_prewarm: bars refresh failed (continuing to scan)")

            # 2. Run all 4 scanners against the (now-fresh) bars cache.
            #    persist=False because we have no user — write directly to
            #    the SHARED ScanCache (no ScanRun history row).
            try:
                from .scanner.runner import run_all_scans, _upsert_scan_cache
                results, per_scan_ms, total_ms, universe_size = run_all_scans(
                    db, persist=False,
                )
                now = datetime.utcnow()
                for scan_type, top in results.items():
                    _upsert_scan_cache(
                        db, scan_type,
                        universe_size=universe_size,
                        candidates_count=len(top),
                        elapsed_ms=per_scan_ms.get(scan_type, 0),
                        top=top, now=now,
                    )
                db.commit()
                log.info(
                    "eod_prewarm scans: total=%dms universe=%d hits=%s",
                    total_ms, universe_size,
                    {k: len(v) for k, v in results.items()},
                )
            except Exception:
                log.exception("eod_prewarm: scan run failed")

            elapsed = int((time.perf_counter() - t0) * 1000)
            log.info("eod_prewarm done in %dms", elapsed)
    finally:
        _leader_lock.release()


def start() -> BackgroundScheduler | None:
    """Start the scheduler. Idempotent — calling twice returns the same
    instance. No-op when ``DISABLE_SCHEDULER=1`` (tests + CI)."""
    global _scheduler
    if os.environ.get("DISABLE_SCHEDULER") == "1":
        log.info("scheduler disabled via DISABLE_SCHEDULER=1")
        return None
    if _scheduler is not None:
        return _scheduler

    sched = BackgroundScheduler(timezone=IST, daemon=True)
    sched.add_job(
        _eod_prewarm,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=35, timezone=IST),
        id="eod_prewarm",
        max_instances=1,
        coalesce=True,  # if missed (e.g. machine asleep), only run once
        replace_existing=True,
    )

    # TSL ladder — runs 15 minutes AFTER eod_prewarm so the bhavcopy +
    # scanner cache are fresh. The runner reads today's close from the
    # bars cache, so it MUST run after eod_prewarm finishes its bars
    # refresh. 15:50 IST gives the prewarm ~15 min headroom.
    sched.add_job(
        _tsl_ladder_run,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=50, timezone=IST),
        id="tsl_ladder",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # Every-minute health probe — drives the public /status page.
    # Gaps in the recorded rows = "app was down, scheduler couldn't write"
    # = visible downtime on the timeline.
    from . import health_monitor as hm
    from apscheduler.triggers.interval import IntervalTrigger
    sched.add_job(
        hm.probe_and_log,
        IntervalTrigger(seconds=hm.PROBE_INTERVAL_S),
        id="health_probe",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    sched.start()
    _scheduler = sched
    log.info(
        "scheduler started: eod_prewarm 15:35 IST, tsl_ladder 15:50 IST mon-fri, "
        "health_probe every %ds",
        hm.PROBE_INTERVAL_S,
    )
    return sched


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def trigger_prewarm_now() -> None:
    """Manual trigger for testing — runs synchronously in the calling thread."""
    _eod_prewarm()


# ---------------------------------------------------------------------------
# TSL ladder runner — Phase E2.
# Wrapped in a leader-elected job so multi-worker setups don't double-run.
# ---------------------------------------------------------------------------

_tsl_lock = threading.Lock()


def _tsl_ladder_run() -> None:
    """Walk every Kite-managed open trade across users, decide ratchet,
    modify_gtt at Kite for any that cross a ladder rung. Logs to
    TslDecision (one row per trade per day, idempotent via composite
    unique index)."""
    if not _tsl_lock.acquire(blocking=False):
        log.info("tsl_ladder skipped: another invocation in progress")
        return
    try:
        from .trading_engine import tsl_runner
        with SessionLocal() as db:
            t0 = time.perf_counter()
            summaries = tsl_runner.run_for_all_users(db)
            elapsed = int((time.perf_counter() - t0) * 1000)
            log.info(
                "tsl_ladder done in %dms across %d users: %s",
                elapsed, len(summaries), summaries,
            )
    except Exception:
        log.exception("tsl_ladder run failed")
    finally:
        _tsl_lock.release()


def trigger_tsl_now() -> None:
    """Manual trigger for testing or admin recovery — synchronous."""
    _tsl_ladder_run()
