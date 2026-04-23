from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import masterlist
from ..db import get_db
from ..deps import templates
from ..models import MasterListItem

router = APIRouter(prefix="/masterlist")


@router.get("", response_class=HTMLResponse)
def masterlist_page(request: Request, db: Session = Depends(get_db)):
    grouped = {}
    for cat in masterlist.CATEGORIES:
        grouped[cat] = (
            db.query(MasterListItem)
            .filter(MasterListItem.category == cat)
            .order_by(MasterListItem.sort_order, MasterListItem.value)
            .all()
        )
    return templates.TemplateResponse(
        request,
        "masterlist.html",
        {"grouped": grouped, "labels": masterlist.CATEGORY_LABELS},
    )


@router.post("/add")
def add(
    db: Session = Depends(get_db),
    category: str = Form(...),
    value: str = Form(...),
):
    masterlist.add_value(db, category, value)
    return RedirectResponse(url="/masterlist", status_code=303)


@router.post("/{item_id}/rename")
def rename(
    item_id: int,
    db: Session = Depends(get_db),
    value: str = Form(...),
):
    row = db.get(MasterListItem, item_id)
    if row is None:
        return RedirectResponse(url="/masterlist", status_code=303)
    new_value = value.strip()
    if new_value:
        row.value = new_value
        db.commit()
    return RedirectResponse(url="/masterlist", status_code=303)


@router.post("/{item_id}/delete")
def remove(item_id: int, db: Session = Depends(get_db)):
    masterlist.delete_value(db, item_id)
    return RedirectResponse(url="/masterlist", status_code=303)
