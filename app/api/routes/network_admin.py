"""The network summary for the dashboard's "Network Command Centre" (BE-4) — Admin and
Management. Aggregates only (app/commissions/network.py documents each figure)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.commissions import network, periods
from app.memory.db import get_db

router = APIRouter(prefix="/api/v1/dashboard")

MAX_PERIOD_DAYS = 366


@router.get("/network/summary")
def network_summary(date_from: Optional[date] = None, date_to: Optional[date] = None,
                    db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Counts and money for the whole network. The period (IST days, inclusive)
    defaults to this month so far and applies to revenue and commission figures."""
    if not employee.can_view_network_summary():
        raise HTTPException(status_code=403, detail="Only Admin or Management can view the network summary.")
    default_from, default_to = network.default_period(periods.today_ist())
    date_from, date_to = date_from or default_from, date_to or default_to
    if date_to < date_from or (date_to - date_from).days > MAX_PERIOD_DAYS:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_period", "field": "date_from",
            "message": f"date_from must be on or before date_to, at most {MAX_PERIOD_DAYS} days apart."})
    return network.summary(db, date_from, date_to, datetime.now(timezone.utc))
