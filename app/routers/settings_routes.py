from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth as user_auth
from .. import kite, settings as app_settings
from ..db import get_db
from ..deps import templates
from ..models import CapitalEvent, KiteInstrument, User

router = APIRouter(prefix="/settings")


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(user_auth.require_user),
):
    events = db.query(CapitalEvent).order_by(CapitalEvent.date.desc()).all()
    instrument_count = db.query(KiteInstrument).count()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": app_settings.all_settings(db),
            "events": events,
            "kite": kite.auth_status(user),
            "kite_instrument_count": instrument_count,
        },
    )


@router.post("/save")
def save(
    db: Session = Depends(get_db),
    starting_capital: float = Form(...),
    starting_capital_date: str = Form(...),
    default_risk_pct: float = Form(...),
    risk_pct_low: float = Form(...),
    max_open_heat_pct: float = Form(...),
    default_allocation_pct: float = Form(...),
):
    # Sanity bounds — silently clamp pathological inputs rather than 500.
    default_risk_pct = max(0.0, min(0.05, default_risk_pct))
    risk_pct_low = max(0.0, min(default_risk_pct, risk_pct_low))
    max_open_heat_pct = max(0.0, min(0.50, max_open_heat_pct))
    default_allocation_pct = max(0.0, min(1.0, default_allocation_pct))

    # Validate starting_capital_date here, not on read. dashboard.build_year
    # silently set sc_date=None on a ValueError, which disabled the anchor
    # logic and caused capital math to silently include pre-anchor P&L.
    # Reject malformed dates at the boundary so the user sees the error.
    sc_date_clean = (starting_capital_date or "").strip()
    if sc_date_clean:
        try:
            date.fromisoformat(sc_date_clean)
        except ValueError:
            raise HTTPException(
                400,
                f"Starting capital date must be YYYY-MM-DD, got {starting_capital_date!r}",
            )

    app_settings.set_value(db, "starting_capital", str(starting_capital))
    app_settings.set_value(db, "starting_capital_date", sc_date_clean)
    app_settings.set_value(db, "default_risk_pct", str(default_risk_pct))
    app_settings.set_value(db, "risk_pct_low", str(risk_pct_low))
    app_settings.set_value(db, "max_open_heat_pct", str(max_open_heat_pct))
    app_settings.set_value(db, "default_allocation_pct", str(default_allocation_pct))
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/capital-event")
def add_event(
    db: Session = Depends(get_db),
    event_date: str = Form(...),
    amount: float = Form(...),
    note: str | None = Form(None),
):
    try:
        d = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError:
        d = date.today()
    db.add(CapitalEvent(date=d, amount=amount, note=note or None))
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/capital-event/{event_id}/edit")
def edit_event(
    event_id: int,
    db: Session = Depends(get_db),
    event_date: str = Form(...),
    amount: float = Form(...),
    note: str | None = Form(None),
):
    row = db.get(CapitalEvent, event_id)
    if row is None:
        return RedirectResponse(url="/settings", status_code=303)
    try:
        row.date = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError:
        pass
    row.amount = amount
    row.note = note or None
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/capital-event/{event_id}/delete")
def delete_event(event_id: int, db: Session = Depends(get_db)):
    row = db.get(CapitalEvent, event_id)
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse(url="/settings", status_code=303)
