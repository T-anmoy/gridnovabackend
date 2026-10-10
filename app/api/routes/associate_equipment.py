"""The equipment catalogue for associates — what they choose from when registering
installed units (app/execution/register.py).

Associate-authenticated (get_current_associate: active associates only). Deliberately a
separate route and shape from the staff GET /api/v1/equipment/: it never returns a price
field of any kind (ITEM_FIELDS is the whole response item). Active items only;
development placeholder rows only outside production. Rate-limited per associate.

When the model on the nameplate is not listed, the associate registers the unit with the
brand and model as printed instead (an unmatched unit) — never stuck.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedAssociate, get_current_associate
from app.config import settings
from app.memory.db import get_db
from app.memory.models import Equipment, EquipmentCategory

router = APIRouter(prefix="/api/v1/associate/equipment")

ITEM_FIELDS = ("id", "category", "brand", "model_name", "specification", "is_dcr_compliant", "is_placeholder")
RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60
_request_log: defaultdict[str, list[datetime]] = defaultdict(list)


def _check_rate_limit(key: str) -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=RATE_WINDOW_SECONDS)
    _request_log[key] = [t for t in _request_log[key] if t > cutoff]
    if len(_request_log[key]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail={"code": "rate_limited",
                                                     "message": "Too many requests. Please wait a moment."})
    _request_log[key].append(now)


def item(e: Equipment) -> dict:
    return {"id": e.id, "category": e.category.value, "brand": e.brand, "model_name": e.model_name,
            "specification": e.specification, "is_dcr_compliant": bool(e.is_dcr_compliant),
            "is_placeholder": bool(e.is_placeholder)}


@router.get("")
def list_equipment(category: Optional[EquipmentCategory] = None,
                   q: Optional[str] = Query(default=None, max_length=100),
                   db: Session = Depends(get_db), current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Active catalogue items. `category`: module | inverter | structure | bos_component.
    `q`: case-insensitive match on brand or model name."""
    _check_rate_limit(f"associate:{current.associate_pk}")
    stmt = select(Equipment).where(Equipment.is_active.is_(True))
    if settings.is_production_like:
        stmt = stmt.where(Equipment.is_placeholder.is_(False))
    if category is not None:
        stmt = stmt.where(Equipment.category == category)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        from sqlalchemy import func
        stmt = stmt.where(or_(func.lower(Equipment.brand).like(like), func.lower(Equipment.model_name).like(like)))
    rows = db.execute(stmt.order_by(Equipment.category, Equipment.brand, Equipment.model_name, Equipment.id)).scalars()
    return {"items": [item(e) for e in rows]}
