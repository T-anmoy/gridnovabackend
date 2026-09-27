"""Routes for associates themselves — authenticated by get_current_associate, never by
the staff dependency. An associate sees their own record; a Preferred Associate also
sees the roster of associates below them in their territory (app/auth/scoping.py
RosterScope)."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.routes.associates_admin import document_link
from app.associates import issuance, service
from app.auth.dependencies import AuthenticatedAssociate, get_current_associate
from app.auth.scoping import roster_scope
from app.memory.db import get_db
from app.memory.models import (
    Associate, AssociateApplication, AssociateStatus, AssociateType, CustomerOnboarding, OnboardingStatus,
)

router = APIRouter(prefix="/api/v1/associate")


@router.get("/me")
def me(db: Session = Depends(get_db), current: AuthenticatedAssociate = Depends(get_current_associate)):
    associate = service.get_associate(db, current.associate_id)
    return {
        "associate_id": associate.associate_id,
        "associate_type": associate.associate_type.value,
        "status": associate.status.value,
        "full_name": associate.full_name,
        "email": associate.email,
        "mobile": associate.mobile,
        "capability": associate.capability.value if associate.capability else None,
        "level": associate.level.value if associate.level else None,
        "territory": issuance.territory_label(db, associate),
        "approved_at": associate.approved_at.isoformat(),
        "documents": {
            "id_card": associate.id_card_storage_key is not None,
            "welcome_letter": associate.welcome_letter_storage_key is not None,
        },
    }


@router.get("/me/documents/{document}")
def my_document(document: Literal["id_card", "welcome_letter"], db: Session = Depends(get_db),
                current: AuthenticatedAssociate = Depends(get_current_associate)):
    """A short-lived signed link to the associate's own ID card or welcome letter."""
    return document_link(service.get_associate(db, current.associate_id), document)


@router.get("/territory/members")
def territory_members(status: Optional[AssociateStatus] = None, associate_type: Optional[AssociateType] = None,
                      page: int = Query(default=1, ge=1), page_size: int = Query(default=25, ge=1, le=100),
                      db: Session = Depends(get_db),
                      current: AuthenticatedAssociate = Depends(get_current_associate)):
    """Every associate in the caller's territory below the caller's level, with how
    many customers each has onboarded. Preferred Associates only (403 otherwise).
    Counts cover all of a member's customers; a customer an Admin closed after an
    attribution conflict is not counted."""
    scope = roster_scope(db.get(Associate, current.associate_pk))
    counts = (select(CustomerOnboarding.owner_associate_id.label("owner_id"),
                     func.count().label("customers"),
                     func.sum(case((CustomerOnboarding.status == OnboardingStatus.verified, 1), else_=0))
                     .label("verified"))
              .where(CustomerOnboarding.status != OnboardingStatus.closed)
              .group_by(CustomerOnboarding.owner_associate_id).subquery())
    q = (select(Associate, func.coalesce(counts.c.customers, 0), func.coalesce(counts.c.verified, 0))
         .outerjoin(AssociateApplication, AssociateApplication.id == Associate.associate_application_id)
         .outerjoin(counts, counts.c.owner_id == Associate.id)
         .where(scope.condition()))
    if status:
        q = q.where(Associate.status == status)
    if associate_type:
        q = q.where(Associate.associate_type == associate_type)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(Associate.full_name, Associate.id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"associate_id": a.associate_id,
                       "full_name": a.full_name,
                       "associate_type": a.associate_type.value,
                       "capability": a.capability.value if a.capability else None,
                       "level": a.level.value if a.level else None,
                       "status": a.status.value,
                       "customer_count": int(customers),
                       "verified_customer_count": int(verified)}
                      for a, customers, verified in rows],
            "page": page, "page_size": page_size, "total": total}
