"""Routes for associates themselves — authenticated by get_current_associate, never by
the staff dependency. An associate sees only their own record."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.routes.associates_admin import document_link
from app.associates import issuance, service
from app.auth.dependencies import AuthenticatedAssociate, get_current_associate
from app.memory.db import get_db

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
