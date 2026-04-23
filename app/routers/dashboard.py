from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import analytics
from .. import calculations as calc
from .. import dashboard as dash
from .. import portfolio
from .. import prices as price_svc
from .. import settings as app_settings
from ..db import get_db
from ..deps import templates
from ..models import CapitalEvent, Trade

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, year: int | None = None, db: Session = Depends(get_db)):
    # `year` is an FY start year (FY 2025-26 = year 2025). Default = current FY.
    year = year if year is not None else dash.fy_start(date.today())
    rows, yagg, equity = dash.build_year(db, year)
    setups = dash.setup_performance(db, year)

    open_trades = (
        db.query(Trade)
        .filter(Trade.status == "open")
        .order_by(Trade.entry_date.desc())
        .all()
    )
    open_heat_total = sum(calc.open_heat_rs(t) for t in open_trades)
    open_exposure_total = sum(calc.open_exposure_rs(t) for t in open_trades)

    settings_map = app_settings.all_settings(db)

    # All FYs with activity for the year-selector.
    available_years = dash.years_with_activity(db)
    if year not in available_years:
        available_years = sorted(set(available_years) | {year})

    # Per-FY monthly breakdown: newest FY first.
    all_years = sorted(dash.years_with_activity(db), reverse=True)
    yearly_tables = []
    for y in all_years:
        y_rows, y_yagg, _ = dash.build_year(db, y)
        if any(r.num_trades or r.added_withdrawn for r in y_rows):
            yearly_tables.append({
                "year": y,
                "label": dash.fy_label(y),
                "rows": y_rows,
                "yagg": y_yagg,
            })

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "year": year,
            "available_years": available_years,
            "months": rows,
            "yagg": yagg,
            "equity_curve": equity,
            "yearly_tables": yearly_tables,
            "setups": setups,
            "open_trades": open_trades,
            "open_heat_total": open_heat_total,
            "open_exposure_total": open_exposure_total,
            "metrics_for": {t.id: calc.metrics(t) for t in open_trades},
            "settings": settings_map,
            "today_iso": date.today().isoformat(),
            "prices_last_refresh": price_svc.last_refresh_at(db),
            "portfolio": portfolio.build(db),
            "edge_setups": analytics.setup_edge(db, year),
            "plan_breakdown": analytics.plan_followed_breakdown(db, year),
            "hold_buckets": analytics.hold_time_buckets(db, year),
            "weekday_stats": analytics.weekday_breakdown(db, year),
            # Sprint 3
            "weekly_summary": analytics.weekly_summary(db, days=7),
            "loss_streak": analytics.consecutive_loss_alert(db, streak_threshold=3),
            "needs_review": analytics.trades_needing_review(db, limit=5),
            "fy_label": dash.fy_label(year),
        },
    )


@router.post("/dashboard/capital-event")
def quick_capital_event(
    db: Session = Depends(get_db),
    kind: str = Form("deposit"),
    event_date: str = Form(...),
    amount: float = Form(...),
    note: str | None = Form(None),
):
    try:
        d = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError:
        d = date.today()
    signed = amount if kind == "deposit" else -abs(amount)
    db.add(CapitalEvent(date=d, amount=signed, note=note or None))
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)
