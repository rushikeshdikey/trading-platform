from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import prices
from ..db import get_db

router = APIRouter(prefix="/prices")


@router.post("/refresh")
def refresh(db: Session = Depends(get_db)):
    summary = prices.refresh_all_open(db)
    failed = ",".join(summary["failed_symbols"])
    return RedirectResponse(
        url=(
            f"/dashboard?refreshed={summary['trades_updated']}"
            f"&checked={summary['symbols_checked']}"
            + (f"&failed={failed}" if failed else "")
        ),
        status_code=303,
    )
