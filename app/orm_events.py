"""SQLAlchemy events that auto-scope per-user models to the current user.

Two events:

1. ``do_orm_execute`` (SELECT) — for every model with a ``user_id`` column,
   inject ``WHERE user_id = <current_user>`` via ``with_loader_criteria``.
   This is the single chokepoint that prevents data leakage across users.

2. ``before_flush`` (INSERT) — for every new instance of a per-user model,
   set ``user_id`` to the current user if it isn't already set. Without
   this, every router INSERT site would have to pass user_id manually.

Escape hatch:
- An execution can opt out via ``stmt.execution_options(skip_user_filter=True)``.
  Use this from admin code that needs cross-user views (e.g. /admin/users).
- Background tasks (no request → contextvar is None) get NO filter applied.
  This is intentional: background tasks should only touch shared market-data
  tables (DailyBar, KiteInstrument, MarketBreadth, InstrumentPrice). If a
  background task needs to write per-user data, it MUST set user_id explicitly.

Why a single dispatch instead of model-level filters: keeping the list of
"per-user models" in ONE place makes it impossible to forget to filter a
new model — adding ``user_id: Mapped[int] = mapped_column(...)`` to a model
is sufficient; the event picks it up automatically.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from . import auth as auth_mod
from . import models as models_mod


def _per_user_models() -> list[type]:
    """All mapped models that have a `user_id` column. Built lazily because
    the import-time module load order can be tricky in tests."""
    out = []
    for name in dir(models_mod):
        obj = getattr(models_mod, name)
        if isinstance(obj, type) and issubclass(obj, models_mod.Base) and obj is not models_mod.Base:
            if hasattr(obj, "user_id"):
                out.append(obj)
    return out


@event.listens_for(Session, "do_orm_execute")
def _filter_per_user(execute_state) -> None:
    if not execute_state.is_select:
        return
    if execute_state.execution_options.get("skip_user_filter"):
        return
    user_id = auth_mod.current_user_id_var.get()
    if user_id is None:
        # No request user (e.g. a background task). Per-user models don't
        # appear in those code paths; if they do, it's a bug — the SELECT
        # returns rows for *every* user.
        return
    for model in _per_user_models():
        # `track_closure_variables=False` tells the lambda-cache to NOT
        # bake the captured `user_id` into the cache key; otherwise the
        # first user's compiled statement gets reused for everyone.
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                model,
                lambda cls: cls.user_id == user_id,
                include_aliases=True,
                track_closure_variables=False,
            )
        )


@event.listens_for(Session, "before_flush")
def _stamp_user_on_insert(session: Session, _flush_context, _instances) -> None:
    user_id = auth_mod.current_user_id_var.get()
    if user_id is None:
        return
    for obj in session.new:
        if hasattr(obj, "user_id") and getattr(obj, "user_id", None) is None:
            obj.user_id = user_id
