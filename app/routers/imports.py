from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import auth as user_auth
from .. import importer, zerodha
from ..db import get_db
from ..deps import templates
from ..models import Trade, User

router = APIRouter(prefix="/import")


@router.get("", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db)):
    trade_count = db.query(Trade).count()
    return templates.TemplateResponse(
        request,
        "import.html",
        {
            "trade_count": trade_count,
            "result": None,
        },
    )


@router.post("/zerodha", response_class=HTMLResponse)
async def import_zerodha(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    mode: str | None = Form(None),  # accepted for back-compat; ignored — always append
):
    raw = await file.read()
    result = zerodha.import_tradebook(db, filename=file.filename or "", raw=raw)
    return templates.TemplateResponse(
        request,
        "import_result.html",
        {
            "trades_created": result.trades_created,
            "trades_extended": result.trades_extended,
            "trades_closed": result.trades_closed,
            "executions_parsed": result.executions_parsed,
            "executions_applied": result.executions_applied,
            "executions_skipped_duplicate": result.executions_skipped_duplicate,
            "symbols_touched": sorted(result.symbols_touched),
            "warnings": result.warnings[:20],
            "more_warnings": max(0, len(result.warnings) - 20),
        },
    )


@router.post("/zerodha/sync-today", response_class=HTMLResponse)
def sync_zerodha_today(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(user_auth.require_user),
):
    """One-click pull of today's Zerodha executions via Kite's trades() API."""
    try:
        result = zerodha.fetch_today_via_kite(db, user)
    except RuntimeError as exc:
        return RedirectResponse(
            url=f"/import?kite_error={exc}", status_code=303
        )
    return templates.TemplateResponse(
        request,
        "import_result.html",
        {
            "trades_created": result.trades_created,
            "trades_extended": result.trades_extended,
            "trades_closed": result.trades_closed,
            "executions_parsed": result.executions_parsed,
            "executions_applied": result.executions_applied,
            "executions_skipped_duplicate": result.executions_skipped_duplicate,
            "symbols_touched": sorted(result.symbols_touched),
            "warnings": result.warnings[:20],
            "more_warnings": max(0, len(result.warnings) - 20),
        },
    )


@router.post("/upload", response_class=HTMLResponse)
async def import_upload(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    with NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        result = importer.import_from_xlsx(db, tmp_path)
        importer.import_capital_from_dashboard(db, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return RedirectResponse(
        url=f"/import?imported={result.get('imported', 0)}&skipped={result.get('skipped', 0)}",
        status_code=303,
    )
