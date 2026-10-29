"""An approved associate whose Clerk invitation never arrived claims their account (BE-21).

They sign up normally at the associate sign-up page with the email on their application,
then call POST /api/v1/associate/me/claim with that (principal-less) Clerk token. The
caller is bound to an associate only when ALL hold:

  * the caller is not staff (no Employee row for this Clerk user);
  * one of the caller's VERIFIED Clerk addresses equals the associate's email
    (normalised) — the token's own email claim is never enough;
  * the associate is active, has no Clerk user yet, and its invitation is `failed` or
    `sent` (an invitation in flight may be overtaken: the claim revokes it).

Then Clerk's public_metadata gets the associate claims (the same ones the invitation would
have carried), `clerk_user_id` is set, a pending invitation is revoked and the claim is
audited. Every refusal is the same 403 claim_not_allowed, which never says whether the
address belongs to an associate.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.associates import service
from app.associates.issuance import associate_claims
from app.memory.models import Associate, AssociateStatus, ClerkInviteStatus, Employee

logger = logging.getLogger(__name__)

CLAIM_NOT_ALLOWED = {
    "code": "claim_not_allowed",
    "message": "This sign-in cannot be linked to a Grid Nova associate account. Sign up with the email address "
               "on your approved application, or ask Grid Nova to check it.",
}
CLAIMABLE_INVITE_STATUSES = (ClerkInviteStatus.failed, ClerkInviteStatus.sent)


def _refuse(clerk_user_id: str, reason: str) -> HTTPException:
    logger.warning("Associate claim refused: %s", reason, extra={"clerk_user_id": clerk_user_id})
    return HTTPException(status_code=403, detail=CLAIM_NOT_ALLOWED)


def bind(db: Session, associate_pk: int, clerk_user_id: str) -> bool:
    """Set clerk_user_id only if still unset — the database settles two claims racing for
    one associate (the loser's UPDATE matches no row). Not committed."""
    result = db.execute(update(Associate)
                        .where(Associate.id == associate_pk, Associate.clerk_user_id.is_(None),
                               Associate.status == AssociateStatus.active)
                        .values(clerk_user_id=clerk_user_id)
                        .execution_options(synchronize_session=False))
    return result.rowcount == 1


def claim(db: Session, payload: dict) -> Associate:
    from app.auth import clerk_client
    from app.staff.invitations import _verified_emails

    clerk_user_id = payload.get("sub") or ""
    if db.get(Employee, clerk_user_id) is not None:
        raise _refuse(clerk_user_id, "caller is staff")
    if db.execute(select(Associate.id).where(Associate.clerk_user_id == clerk_user_id)).first():
        raise _refuse(clerk_user_id, "caller already holds an associate account")
    verified = _verified_emails(clerk_user_id, payload)
    if not verified:
        raise _refuse(clerk_user_id, "no verified email")
    candidates = list(db.execute(select(Associate).where(
        func.lower(func.trim(Associate.email)).in_(sorted(verified)),
        Associate.status == AssociateStatus.active,
        Associate.clerk_user_id.is_(None),
        Associate.clerk_invite_status.in_(CLAIMABLE_INVITE_STATUSES),
    )).scalars())
    if len(candidates) != 1:
        raise _refuse(clerk_user_id, f"{len(candidates)} claimable associates match")
    associate = candidates[0]
    if not bind(db, associate.id, clerk_user_id):
        db.rollback()
        raise _refuse(clerk_user_id, "already claimed")
    try:
        clerk_client.update_public_metadata(clerk_user_id, associate_claims(associate))
    except clerk_client.ClerkSyncError:
        db.rollback()   # unbound again: the claim can be retried
        raise HTTPException(status_code=502, detail={
            "code": "clerk_sync_failed",
            "message": "We could not finish linking your account with Clerk. Try again in a minute."})
    invitation_revoked = False
    if associate.clerk_invite_status == ClerkInviteStatus.sent and associate.clerk_invitation_id:
        try:
            invitation_revoked = clerk_client.revoke_invitation(associate.clerk_invitation_id)
        except clerk_client.ClerkSyncError:   # already accepted or expired: nothing to stop
            logger.info("Clerk invitation for %s not revoked at claim", associate.associate_id)
    db.refresh(associate)
    service.audit(db, "associate", associate.associate_id, "clerk_user_claimed", clerk_user_id,
                  before={"clerk_user_id": None},
                  after={"clerk_user_id": clerk_user_id, "invitation_revoked": invitation_revoked,
                         "clerk_invite_status": associate.clerk_invite_status.value})
    db.commit()
    return associate
