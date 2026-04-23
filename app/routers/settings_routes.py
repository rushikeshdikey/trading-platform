from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import kite, settings as app_settings
from ..db import get_db
from ..deps import templates
from ..models import CapitalEvent, KiteInstrument

router = APIRouter(prefix="/settings")


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    events = db.query(CapitalEvent).order_by(CapitalEvent.date.desc()).all()
    instrument_count = db.query(KiteInstrument).count()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": app_settings.all_settings(db),
            "events": events,
            "kite": kite.auth_status(db),
            "kite_instrument_count": instrument_count,
        },
    )


@router.post("/save")
def save(
    db: Session = Depends(get_db),
    starting_capital: float = Form(...),
    starting_capital_date: str = Form(...),
    default_risk_pct: float = Form(...),
    default_allocation_pct: float = Form(...),
):
    app_settings.set_value(db, "starting_capital", str(starting_capital))
    app_settings.set_value(db, "starting_capital_date", starting_capital_date)
    app_settings.set_value(db, "default_risk_pct", str(default_risk_pct))
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
