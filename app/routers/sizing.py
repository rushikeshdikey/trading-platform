from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import calculations as calc
from .. import dashboard as dash
from .. import settings as app_settings
from ..db import get_db
from ..deps import templates

router = APIRouter(prefix="/sizing")


def _sl_price(entry: float, sl_pct_whole: float, side: str = "B") -> float:
    """Compute SL absolute price from entry and SL distance (%).

    ``sl_pct_whole`` is whole-percent (2.5 for 2.5%), not a fraction.
    Longs: SL below entry. Shorts: SL above.
    """
    frac = (sl_pct_whole or 0) / 100.0
    if side == "S":
        return entry * (1 + frac)
    return entry * (1 - frac)


@router.get("", response_class=HTMLResponse)
def sizing_page(request: Request, db: Session = Depends(get_db)):
    capital = dash.current_capital(db)
    # Stored as fractions (0.005, 0.10, 0.025); UI uses whole percent (0.5, 10, 2.5).
    risk_pct = app_settings.get_float(db, "default_risk_pct", 0.005) * 100
    alloc_pct = app_settings.get_float(db, "default_allocation_pct", 0.10) * 100
    sl_pct = app_settings.get_float(db, "default_sl_pct", 0.025) * 100
    return templates.TemplateResponse(
        request,
        "sizing.html",
        {
            "capital": capital,
            "risk_pct": risk_pct,
            "alloc_pct": alloc_pct,
            "sl_pct": sl_pct,
            "entry": None,
            "sl": None,
            "sl_price": None,
            "result_risk": None,
            "result_alloc": None,
        },
    )


@router.post("/calc", response_class=HTMLResponse)
def calculate(
    request: Request,
    db: Session = Depends(get_db),
    capital: float = Form(...),
    risk_pct: float = Form(...),  # whole percent (e.g. 0.5)
    alloc_pct: float = Form(...),  # whole percent (e.g. 10)
    sl_pct: float = Form(...),  # whole percent (e.g. 2.5)
    entry: float = Form(...),
    side: str = Form("B"),
):
    sl_price = _sl_price(entry, sl_pct, side)
    result_risk = calc.size_by_risk(capital, risk_pct / 100.0, entry, sl_price)
    result_alloc = calc.size_by_allocation(capital, alloc_pct / 100.0, entry, sl_price)
    return templates.TemplateResponse(
        request,
        "partials/sizing_result.html",
        {
            "capital": capital,
            "risk_pct": risk_pct,
            "alloc_pct": alloc_pct,
            "sl_pct": sl_pct,
            "entry": entry,
            "sl_price": sl_price,
            "side": side,
            "result_risk": result_risk,
            "result_alloc": result_alloc,
        },
    )
