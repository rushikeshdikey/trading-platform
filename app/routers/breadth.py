"""Market breadth dashboard — participation + cap-segment leadership."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import breadth
from ..db import get_db
from ..deps import templates

router = APIRouter(prefix="/breadth")


RANGE_DAYS = {"15D": 15, "1M": 31, "3M": 93, "6M": 186, "1Y": 366, "2Y": 730, "5Y": 1826, "All": 7300}

SEGMENT_CHOICES = ("all", "large", "mid", "small")


@router.get("", response_class=HTMLResponse)
def breadth_page(
    request: Request,
    range: str = "3M",
    segment: str = "all",
    db: Session = Depends(get_db),
):
    if segment not in SEGMENT_CHOICES:
        segment = "all"
    days = RANGE_DAYS.get(range, 93)
    rows = breadth.series_for(db, days=days, universe=segment)
    latest = breadth.latest(db, universe=segment)

    # Side-by-side snapshot per segment — used by the "cap leadership" strip
    # so the user sees which slice is leading/lagging at a glance.
    segment_snapshot = []
    for seg in SEGMENT_CHOICES:
        row = breadth.latest(db, universe=seg)
        segment_snapshot.append({
            "key": seg,
            "label": breadth.UNIVERSE_LABELS[seg],
            "row": row,
        })

    sentiment_label = sentiment_class = None
    if latest is not None:
        sentiment_label, sentiment_class = breadth.sentiment_label(
            latest.pct_above_200ema, latest.pct_above_50ema
        )

    # Distribution-day count (O'Neil's institutional-pulse signal). Read
    # from the nifty_daily table — separate fetcher in nifty_index.py
    # populates it during the EOD prewarm. Empty dict if no Nifty bars yet.
    from ..scanner import nifty_index as ni_mod
    try:
        distribution = ni_mod.count_distribution_days(db)
    except Exception:  # noqa: BLE001
        distribution = {}

    return templates.TemplateResponse(
        request,
        "breadth.html",
        {
            "rows": rows,
            "latest": latest,
            "range": range,
            "segment": segment,
            "segment_label": breadth.UNIVERSE_LABELS[segment],
            "segment_choices": SEGMENT_CHOICES,
            "segment_labels": breadth.UNIVERSE_LABELS,
            "segment_snapshot": segment_snapshot,
            "sentiment_label": sentiment_label,
            "sentiment_class": sentiment_class,
            "range_options": list(RANGE_DAYS.keys()),
            "universe_size": len(breadth.universe_symbols(segment)),
            "distribution": distribution,
        },
    )


@router.post("/refresh")
def refresh(days: int = 130, db: Session = Depends(get_db)):
    """Fetch the full cap union once, recompute breadth for every segment."""
    try:
        summary = breadth.compute_and_store(db, days=days)
        return RedirectResponse(
            url=f"/breadth?refreshed={summary['rows_written']}&symbols={summary.get('symbols_seen', 0)}",
            status_code=303,
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            url=f"/breadth?error={type(exc).__name__}: {exc}",
            status_code=303,
        )
