from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import calculations as calc
from .. import masterlist
from .. import market_data
from .. import postmortem
from .. import prices as price_svc
from .. import settings as app_settings
from ..db import get_db
from ..deps import templates
from ..models import Exit, Pyramid, Trade

router = APIRouter(prefix="/trades")


def _parse_date(s: str | None, default: date | None = None) -> date:
    if not s:
        return default or date.today()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return default or date.today()


def _opt_float(s: str | None) -> float | None:
    """HTML forms send empty optional fields as '' not absent — coerce to None
    before float parsing so FastAPI's type coercion doesn't 422."""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _next_trade_no(db: Session) -> int:
    highest = db.query(Trade.trade_no).order_by(Trade.trade_no.desc()).first()
    if highest and highest[0]:
        return int(highest[0]) + 1
    return 1


@router.get("", response_class=HTMLResponse)
def list_trades(
    request: Request,
    status: str = "open",
    setup: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(Trade)
    if status in ("open", "closed"):
        q = q.filter(Trade.status == status)
    if setup:
        q = q.filter(Trade.setup == setup)
    trades = q.order_by(Trade.entry_date.desc(), Trade.id.desc()).all()
    metrics_for = {t.id: calc.metrics(t) for t in trades}
    return templates.TemplateResponse(
        request,
        "trades_list.html",
        {
            "trades": trades,
            "metrics_for": metrics_for,
            "status": status,
            "setup": setup,
            "dropdowns": masterlist.all_dropdowns(db),
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_trade_page(
    request: Request,
    db: Session = Depends(get_db),
    instrument: str | None = None,
    entry_price: float | None = None,
    sl: float | None = None,
    qty: int | None = None,
    setup: str | None = None,
):
    """Prefillable new-trade form.

    Query params let the scanner and watchlist deep-link into this page with
    entry/SL/qty populated so the user doesn't retype. All params are optional.
    """
    capital = app_settings.get_float(db, "starting_capital", 1_000_000.0)
    default_risk = app_settings.get_float(db, "default_risk_pct", 0.005)
    default_alloc = app_settings.get_float(db, "default_allocation_pct", 0.10)
    return templates.TemplateResponse(
        request,
        "trade_new.html",
        {
            "dropdowns": masterlist.all_dropdowns(db),
            "capital": capital,
            "default_risk": default_risk,
            "default_alloc": default_alloc,
            "today": date.today().isoformat(),
            "prefill_instrument": (instrument or "").upper(),
            "prefill_entry": entry_price if entry_price is not None else 0,
            "prefill_sl": sl if sl is not None else 0,
            "prefill_qty": qty if qty is not None else 0,
            "prefill_setup": setup or "",
        },
    )


@router.post("/new")
def create_trade(
    request: Request,
    db: Session = Depends(get_db),
    instrument: str = Form(...),
    side: str = Form("B"),
    entry_date: str = Form(...),
    entry_price: float = Form(...),
    initial_qty: int = Form(...),
    sl: float = Form(...),
    setup: str | None = Form(None),
    base_duration: str | None = Form(None),
    strike: str | None = Form(None),
    option_type: str | None = Form(None),
    tsl: str | None = Form(None),
    cmp_price: str | None = Form(None),
    observations: str | None = Form(None),
):
    # Sprint-2 guardrail: reject trades where SL doesn't define meaningful
    # risk. These produce trillion-R averages and undercount risk in the heat
    # meter — silently accepting them is how bad data gets in.
    side_norm = side.strip().upper()[:1]
    if abs(entry_price - sl) < 0.01:
        raise HTTPException(400, "Stop-loss cannot equal entry price. Set a real SL.")
    if side_norm == "B" and sl >= entry_price:
        raise HTTPException(400, "For a long trade, SL must be below entry price.")
    if side_norm == "S" and sl <= entry_price:
        raise HTTPException(400, "For a short trade, SL must be above entry price.")

    trade = Trade(
        trade_no=_next_trade_no(db),
        instrument=instrument.strip().upper(),
        side=side_norm,
        entry_date=_parse_date(entry_date),
        initial_entry_price=entry_price,
        initial_qty=initial_qty,
        sl=sl,
        tsl=_opt_float(tsl),
        cmp=_opt_float(cmp_price),
        setup=setup or None,
        base_duration=base_duration or None,
        strike=_opt_float(strike),
        option_type=option_type or None,
        observations=observations or None,
        status="open",
    )
    db.add(trade)
    db.commit()
    # If the user didn't type a CMP, try to fetch one so the dashboard shows a
    # move/R:R immediately. Failures are silent — user can still add manually.
    if trade.cmp is None:
        try:
            price_svc.refresh_trades(db, [trade])
        except Exception:  # noqa: BLE001
            pass
    return RedirectResponse(url=f"/trades/{trade.id}", status_code=303)


@router.get("/{trade_id}/edit", response_class=HTMLResponse)
def trade_edit_page(trade_id: int, request: Request, db: Session = Depends(get_db)):
    trade = db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "trade_edit.html",
        {
            "trade": trade,
            "dropdowns": masterlist.all_dropdowns(db),
        },
    )


@router.post("/{trade_id}/edit")
def trade_edit_save(
    trade_id: int,
    db: Session = Depends(get_db),
    instrument: str = Form(...),
    side: str = Form("B"),
    entry_date: str = Form(...),
    entry_price: float = Form(...),
    initial_qty: int = Form(...),
    sl: float = Form(...),
    setup: str | None = Form(None),
    base_duration: str | None = Form(None),
    strike: str | None = Form(None),
    option_type: str | None = Form(None),
    tsl: str | None = Form(None),
    cmp_price: str | None = Form(None),
    charges_rs: str | None = Form(None),
    observations: str | None = Form(None),
):
    trade = db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404)
    trade.instrument = instrument.strip().upper()
    trade.side = side.strip().upper()[:1]
    trade.entry_date = _parse_date(entry_date, trade.entry_date)
    trade.initial_entry_price = entry_price
    trade.initial_qty = initial_qty
    trade.sl = sl
    trade.tsl = _opt_float(tsl)
    trade.cmp = _opt_float(cmp_price)
    trade.charges_rs = _opt_float(charges_rs)
    trade.setup = setup or None
    trade.base_duration = base_duration or None
    trade.strike = _opt_float(strike)
    trade.option_type = option_type or None
    trade.observations = observations or None
    db.commit()
    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)


@router.post("/{trade_id}/pyramid/{pyramid_id}/edit")
def edit_pyramid(
    trade_id: int,
    pyramid_id: int,
    db: Session = Depends(get_db),
    price: float = Form(...),
    qty: int = Form(...),
    pyramid_date: str = Form(...),
):
    row = db.get(Pyramid, pyramid_id)
    if row is None or row.trade_id != trade_id:
        raise HTTPException(404)
    row.price = price
    row.qty = qty
    row.date = _parse_date(pyramid_date, row.date)
    db.commit()
    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)


@router.post("/{trade_id}/exit/{exit_id}/edit")
def edit_exit(
    trade_id: int,
    exit_id: int,
    db: Session = Depends(get_db),
    price: float = Form(...),
    qty: int = Form(...),
    exit_date: str = Form(...),
):
    row = db.get(Exit, exit_id)
    if row is None or row.trade_id != trade_id:
        raise HTTPException(404)
    trade = db.get(Trade, trade_id)
    assert trade is not None
    # Prevent exits that would exceed total qty (subtract old qty first).
    old_qty = row.qty
    row.qty = qty
    row.price = price
    row.date = _parse_date(exit_date, row.date)
    # Re-evaluate close state
    if calc.open_qty(trade) == 0 and trade.exits:
        trade.status = "closed"
        trade.close_date = max(e.date for e in trade.exits)
    else:
        trade.status = "open"
        trade.close_date = None
    db.commit()
    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)


@router.get("/{trade_id}", response_class=HTMLResponse)
def trade_detail(trade_id: int, request: Request, db: Session = Depends(get_db)):
    from .. import charges as charges_svc
    trade = db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404)
    m = calc.metrics(trade)
    capital = app_settings.get_float(db, "starting_capital", 1_000_000.0)
    trade_charges = charges_svc.charges_for(trade)
    estimated_charges = charges_svc.estimate_charges(trade)
    return templates.TemplateResponse(
        request,
        "trade_detail.html",
        {
            "trade": trade,
            "m": m,
            "dropdowns": masterlist.all_dropdowns(db),
            "capital": capital,
            "today": date.today().isoformat(),
            "charges_rs": trade_charges,
            "charges_estimated": estimated_charges,
            "charges_breakdown": charges_svc.breakdown(trade),
            "charges_is_estimate": trade.charges_rs is None,
            "net_pnl_rs": m.pnl_rs - trade_charges,
            "pf_impact": ((m.pnl_rs - trade_charges) / capital) if capital else 0.0,
        },
    )


@router.get("/{trade_id}/chart-data")
def chart_data(trade_id: int, db: Session = Depends(get_db)):
    """OHLC + entry/exit/pyramid/SL markers for the trade-detail chart."""
    trade = db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404)

    # Window: 15 trading days before entry → 15 after close (or today).
    window_start = trade.entry_date - timedelta(days=25)
    window_end = (trade.close_date or date.today()) + timedelta(days=15)
    bars = market_data.fetch_ohlc(db, trade.instrument, window_start, window_end)

    entry_markers = [
        {"date": trade.entry_date.isoformat(), "price": trade.initial_entry_price,
         "qty": trade.initial_qty, "kind": "entry", "label": f"Entry {trade.initial_qty}@{trade.initial_entry_price}"}
    ]
    for p in trade.pyramids:
        entry_markers.append({
            "date": p.date.isoformat(), "price": p.price, "qty": p.qty, "kind": "pyramid",
            "label": f"Pyramid +{p.qty}@{p.price}",
        })
    exit_markers = [
        {"date": e.date.isoformat(), "price": e.price, "qty": e.qty, "kind": "exit",
         "label": f"Exit {e.qty}@{e.price}"}
        for e in trade.exits
    ]

    what_if = postmortem.compute(trade, bars)

    return JSONResponse({
        "instrument": trade.instrument,
        "side": trade.side,
        "sl": trade.sl,
        "tsl": trade.tsl,
        "avg_entry": calc.avg_entry(trade),
        "avg_exit": calc.avg_exit(trade),
        "entry_date": trade.entry_date.isoformat(),
        "close_date": trade.close_date.isoformat() if trade.close_date else None,
        "bars": bars,
        "entries": entry_markers,
        "exits": exit_markers,
        "what_if": {
            "has_data": what_if.has_data,
            "mfe_price": what_if.mfe_price, "mfe_date": what_if.mfe_date, "mfe_r": what_if.mfe_r,
            "mae_price": what_if.mae_price, "mae_date": what_if.mae_date, "mae_r": what_if.mae_r,
            "realised_r": what_if.realised_r,
            "pnl_realised": what_if.pnl_realised,
            "pnl_if_hit_sl": what_if.pnl_if_hit_sl,
            "pnl_if_held_mfe": what_if.pnl_if_held_mfe,
            "pnl_if_hit_3r": what_if.pnl_if_hit_3r,
            "pnl_left_on_table": what_if.pnl_left_on_table,
            "mae_tested_sl": what_if.mae_tested_sl,
        },
    })


@router.post("/{trade_id}/update-live")
def update_live(
    trade_id: int,
    db: Session = Depends(get_db),
    tsl: str | None = Form(None),
    cmp_price: str | None = Form(None),
    sl: str | None = Form(None),
):
    trade = db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404)
    tsl_v = _opt_float(tsl)
    cmp_v = _opt_float(cmp_price)
    sl_v = _opt_float(sl)
    # None for cmp/tsl means "clear the field"; None for sl means "leave as-is"
    # (SL is required on the model, so we shouldn't nuke it from this form).
    trade.tsl = tsl_v
    trade.cmp = cmp_v
    if sl_v is not None:
        trade.sl = sl_v
    db.commit()
    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)


@router.post("/{trade_id}/pyramid")
def add_pyramid(
    trade_id: int,
    db: Session = Depends(get_db),
    price: float = Form(...),
    qty: int = Form(...),
    pyramid_date: str = Form(...),
):
    trade = db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404)
    seq = (
        max((p.sequence for p in trade.pyramids), default=0) + 1 if trade.pyramids else 1
    )
    trade.pyramids.append(
        Pyramid(sequence=seq, price=price, qty=qty, date=_parse_date(pyramid_date))
    )
    db.commit()
    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)


@router.post("/{trade_id}/exit")
def add_exit(
    trade_id: int,
    db: Session = Depends(get_db),
    price: float = Form(...),
    qty: int = Form(...),
    exit_date: str = Form(...),
):
    trade = db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404)

    remaining = calc.open_qty(trade)
    if qty > remaining:
        raise HTTPException(400, f"Can't exit {qty} — only {remaining} open")

    seq = (
        max((e.sequence for e in trade.exits), default=0) + 1 if trade.exits else 1
    )
    trade.exits.append(
        Exit(sequence=seq, price=price, qty=qty, date=_parse_date(exit_date))
    )
    # auto-close if fully exited
    if calc.open_qty(trade) == 0:
        trade.status = "closed"
        trade.close_date = max(e.date for e in trade.exits)
    db.commit()
    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)


@router.post("/{trade_id}/review")
def save_review(
    trade_id: int,
    db: Session = Depends(get_db),
    plan_followed: str | None = Form(None),
    exit_trigger: str | None = Form(None),
    proficiency: list[str] | None = Form(None),
    growth_areas: list[str] | None = Form(None),
    observations: str | None = Form(None),
):
    trade = db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404)
    trade.plan_followed = (plan_followed == "yes") if plan_followed in ("yes", "no") else None
    trade.exit_trigger = exit_trigger or None
    trade.proficiency = ", ".join(proficiency) if proficiency else None
    trade.growth_areas = ", ".join(growth_areas) if growth_areas else None
    trade.observations = observations or None
    db.commit()
    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)


@router.post("/{trade_id}/delete")
def delete_trade(trade_id: int, db: Session = Depends(get_db)):
    trade = db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404)
    db.delete(trade)
    db.commit()
    return RedirectResponse(url="/trades", status_code=303)


@router.post("/{trade_id}/pyramid/{pyramid_id}/delete")
def delete_pyramid(trade_id: int, pyramid_id: int, db: Session = Depends(get_db)):
    row = db.get(Pyramid, pyramid_id)
    if row and row.trade_id == trade_id:
        db.delete(row)
        db.commit()
    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)


@router.post("/{trade_id}/exit/{exit_id}/delete")
def delete_exit(trade_id: int, exit_id: int, db: Session = Depends(get_db)):
    row = db.get(Exit, exit_id)
    if row and row.trade_id == trade_id:
        db.delete(row)
        db.flush()  # ensure the deletion is visible to the refreshed relationship
        trade = db.get(Trade, trade_id)
        if trade is not None:
            db.refresh(trade)
            if calc.open_qty(trade) > 0:
                trade.status = "open"
                trade.close_date = None
        db.commit()
    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)
