"""Subsystem health snapshot — feeds the public /health page.

Each subsystem reports a status (ok / warn / down), one headline metric,
and a sub-line. The page renders them as cards similar in style to the
public /status uptime timeline, but for "current state" rather than
"recent history".

Kept independent from health_monitor.py — that file drives the 60-second
probe loop and the rolling timeline. This file is read-only: it inspects
in-memory state from other modules + a couple of cheap DB queries.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class Subsystem:
    """One row in the health page."""
    key: str                      # 'database', 'scheduler', ...
    name: str                     # 'Database', 'Scheduler', ...
    status: str                   # 'ok' | 'warn' | 'down'
    headline: str                 # big label, e.g. "Connected · 4 ms"
    detail: str = ""              # sub-line, e.g. "Postgres 16 · pool 3/5"
    metrics: dict[str, Any] = field(default_factory=dict)  # raw values for JSON

    @property
    def color(self) -> str:
        return {"ok": "green", "warn": "amber", "down": "red"}.get(self.status, "amber")


@dataclass
class HealthSnapshot:
    overall: str                  # 'ok' | 'warn' | 'down' — worst of all subsystems
    generated_at: datetime
    subsystems: list[Subsystem]

    @property
    def color(self) -> str:
        return {"ok": "green", "warn": "amber", "down": "red"}.get(self.overall, "amber")

    @property
    def headline(self) -> str:
        if self.overall == "ok":
            return "All systems operational"
        if self.overall == "warn":
            return "Some systems degraded"
        return "Service is down"


def _check_database(db: Session) -> Subsystem:
    started = time.perf_counter()
    try:
        db.execute(func.count().select())
        ms = int((time.perf_counter() - started) * 1000)
        return Subsystem(
            key="database",
            name="Database",
            status="ok" if ms < 200 else "warn",
            headline=f"Connected · {ms} ms",
            detail="Postgres connection pool" if "postgres" in str(db.bind.url) else "SQLite",
            metrics={"latency_ms": ms},
        )
    except Exception as exc:  # noqa: BLE001
        return Subsystem(
            key="database",
            name="Database",
            status="down",
            headline="Connection failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
            metrics={"error": str(exc)[:200]},
        )


def _check_scheduler() -> Subsystem:
    """APScheduler liveness — running + jobs registered + next-fire reasonable."""
    try:
        from . import jobs as jobs_mod
        sched = jobs_mod._scheduler
        if sched is None:
            return Subsystem(
                key="scheduler",
                name="Scheduler",
                status="warn",
                headline="Not started",
                detail="APScheduler is not running (DISABLE_SCHEDULER=1 in tests).",
            )
        if not sched.running:
            return Subsystem(
                key="scheduler",
                name="Scheduler",
                status="down",
                headline="Stopped",
                detail="Scheduler exists but is not running.",
            )
        all_jobs = sched.get_jobs()
        eod = next((j for j in all_jobs if j.id == "eod_prewarm"), None)
        probe = next((j for j in all_jobs if j.id == "health_probe"), None)
        next_eod = eod.next_run_time if eod else None
        next_probe = probe.next_run_time if probe else None

        # Health probe should fire every minute. If next-fire is >2 min out,
        # something is wrong.
        status = "ok"
        warn_reason = ""
        if next_probe is None:
            status = "warn"
            warn_reason = "health_probe job missing"
        elif (next_probe - datetime.now(next_probe.tzinfo)).total_seconds() > 120:
            status = "warn"
            warn_reason = "health_probe overdue"

        eod_str = next_eod.astimezone(IST).strftime("%a %H:%M IST") if next_eod else "—"
        return Subsystem(
            key="scheduler",
            name="Scheduler",
            status=status,
            headline=f"{len(all_jobs)} jobs · next EOD {eod_str}",
            detail=warn_reason or f"Health probe every 60 s · EOD pre-warm 15:35 IST mon-fri",
            metrics={
                "running": True,
                "job_count": len(all_jobs),
                "next_eod_utc": next_eod.astimezone(timezone.utc).isoformat() if next_eod else None,
                "next_probe_utc": next_probe.astimezone(timezone.utc).isoformat() if next_probe else None,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return Subsystem(
            key="scheduler",
            name="Scheduler",
            status="down",
            headline="Inspection failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def _check_bars_cache(db: Session) -> Subsystem:
    """Bars-cache health — row count, freshness, in-flight refresh state."""
    try:
        from .models import DailyBar
        from .scanner import bars_cache as bc

        rows = db.query(func.count(DailyBar.id)).scalar() or 0
        symbols = db.query(func.count(func.distinct(DailyBar.symbol))).scalar() or 0
        latest = db.query(func.max(DailyBar.date)).scalar()
        refresh = bc.refresh_status()

        # Status: down if cache is empty / shallow; warn if stale (>5 days);
        # ok otherwise.
        status = "ok"
        warn = ""
        if rows < 200_000:
            status = "warn"
            warn = "shallow cache — auto-backfill running"
        if latest is not None:
            stale_days = (datetime.utcnow().date() - latest).days
            if stale_days > 5:
                status = "warn"
                warn = f"latest bar is {stale_days} days old"

        running = bool(refresh.get("running"))
        if running:
            warn = (warn + " · " if warn else "") + "refresh in progress"

        latest_str = latest.isoformat() if latest else "—"
        return Subsystem(
            key="bars_cache",
            name="Bars cache",
            status=status,
            headline=f"{rows:,} rows · {symbols:,} symbols",
            detail=warn or f"Latest bar: {latest_str}",
            metrics={
                "rows": rows,
                "symbols": symbols,
                "latest_bar_date": latest_str,
                "refresh_running": running,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return Subsystem(
            key="bars_cache",
            name="Bars cache",
            status="down",
            headline="Inspection failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def _check_scanner_cache(db: Session) -> Subsystem:
    """ScanCache freshness."""
    try:
        from .models import ScanCache
        rows = db.query(ScanCache).all()
        if not rows:
            return Subsystem(
                key="scanner_cache",
                name="Scanner cache",
                status="warn",
                headline="No scans cached",
                detail="Click Run-all-live or wait for the 15:35 IST pre-warm.",
            )
        latest = max(r.run_at for r in rows)
        age_min = int((datetime.utcnow() - latest).total_seconds() // 60)
        total_candidates = sum(r.candidates_count for r in rows)
        status = "ok"
        if age_min > 24 * 60:
            status = "warn"
        return Subsystem(
            key="scanner_cache",
            name="Scanner cache",
            status=status,
            headline=f"{len(rows)} scanners · {total_candidates} candidates",
            detail=f"Last refresh {age_min // 60}h {age_min % 60}m ago",
            metrics={
                "scan_count": len(rows),
                "candidate_total": total_candidates,
                "latest_run_utc": latest.isoformat(),
                "age_minutes": age_min,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return Subsystem(
            key="scanner_cache",
            name="Scanner cache",
            status="down",
            headline="Inspection failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def _check_probe_loop(db: Session) -> Subsystem:
    """Most recent /health probe row — gap to now indicates loop liveness."""
    try:
        from .models import HealthCheck
        latest = (
            db.query(HealthCheck)
            .order_by(HealthCheck.checked_at.desc())
            .first()
        )
        if latest is None:
            return Subsystem(
                key="probe_loop",
                name="Health probe",
                status="warn",
                headline="No probes recorded yet",
                detail="The 60-second probe loop hasn't run since this container booted.",
            )
        age_s = int((datetime.utcnow() - latest.checked_at).total_seconds())
        status = "ok" if age_s < 120 else ("warn" if age_s < 300 else "down")
        return Subsystem(
            key="probe_loop",
            name="Health probe",
            status=status,
            headline=f"Last {latest.response_ms} ms · {age_s}s ago",
            detail=f"Probes every 60 s · drives /status timeline",
            metrics={
                "last_response_ms": latest.response_ms,
                "last_checked_utc": latest.checked_at.isoformat(),
                "last_ok": bool(latest.ok),
                "age_seconds": age_s,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return Subsystem(
            key="probe_loop",
            name="Health probe",
            status="down",
            headline="Inspection failed",
            detail=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def build_snapshot(db: Session) -> HealthSnapshot:
    subs = [
        Subsystem(
            key="api",
            name="Web / API",
            status="ok",
            headline="Responding",
            detail="If you're reading this, the FastAPI process is up.",
        ),
        _check_database(db),
        _check_scheduler(),
        _check_probe_loop(db),
        _check_bars_cache(db),
        _check_scanner_cache(db),
    ]
    # Overall = worst subsystem.
    rank = {"ok": 0, "warn": 1, "down": 2}
    overall = max(subs, key=lambda s: rank[s.status]).status
    return HealthSnapshot(
        overall=overall,
        generated_at=datetime.utcnow(),
        subsystems=subs,
    )
