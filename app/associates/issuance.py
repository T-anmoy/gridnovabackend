"""What happens after an approval commits: the Clerk invitation and the two PDFs.

Both run after the approval is committed, never before. An invitation sent before
the commit could survive a rolled-back approval — and because the Associate ID
counter rolls back with it, that ID would later be issued to someone else while a
live invitation still carried it. So the associate exists first; a failure here
leaves an approved associate with a recorded failure status, which an Admin
retries from the dashboard (same pattern as proposal PDFs: the approval stands,
the PDF is retried separately). Each function commits its own outcome.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.associates import documents, service, texts
from app.config import settings
from app.memory.models import (
    Associate, AssociateStatus, ClerkInviteStatus, TerritoryBlock, TerritoryDistrict, TerritoryState,
)

logger = logging.getLogger(__name__)


def associate_claims(associate: Associate) -> dict:
    """The Clerk public_metadata an associate's session token must carry."""
    from app.auth.dependencies import ASSOCIATE_PRINCIPAL

    return {"principal": ASSOCIATE_PRINCIPAL, "associate_id": associate.associate_id}


def send_clerk_invite(db: Session, associate: Associate, performed_by: str, *, resend: bool = False) -> ClerkInviteStatus:
    """Send the Clerk invitation. `resend` (the dashboard's button, BE-19): an invitation
    already sent but never accepted — expired, lost, sent to an address since corrected —
    is revoked and a new one sent. Once the associate has signed in there is nothing to
    send. The post-approval job never resends."""
    from app.auth.clerk_client import ClerkSyncError, create_associate_invitation, revoke_invitation

    if associate.status != AssociateStatus.active:
        raise service.InvalidTransitionError("Only an active associate can be invited.")
    if associate.clerk_user_id is not None:
        raise service.InvalidTransitionError("This associate has already signed in; there is no invitation to send.")
    if associate.clerk_invite_status == ClerkInviteStatus.sent:
        if not resend:
            raise service.InvalidTransitionError("This associate's Clerk invitation was already sent.")
        if associate.clerk_invitation_id:
            try:
                revoke_invitation(associate.clerk_invitation_id)
            except ClerkSyncError:   # already accepted, expired or revoked: Clerk refuses; send anew regardless
                logger.info("Old Clerk invitation for %s not revoked", associate.associate_id)
    try:
        created, invitation_id = create_associate_invitation(
            associate.email, associate_claims(associate), settings.associate_invite_redirect_url,
        )
        outcome, error = (ClerkInviteStatus.sent if created else ClerkInviteStatus.skipped), None
    except ClerkSyncError as exc:
        outcome, invitation_id, error = ClerkInviteStatus.failed, None, str(exc)
    service.record_clerk_invite(db, associate, outcome, invitation_id, performed_by, error)
    db.commit()
    return outcome


def issue_after_approval(associate_pk: int, performed_by: str, issued_on: date) -> None:
    """The approval's follow-up — the Clerk invitation, then the ID card and welcome
    letter — in its own session, on the background runner (app/jobs/runner.py): two
    PDF renders, two uploads and a Clerk call no longer hold up the approval response.
    Each step records its own outcome, exactly as when it ran inline; a failure is
    retried from the dashboard (POST .../clerk-invite, .../documents/regenerate)."""
    from app.memory.db import SessionLocal

    db = SessionLocal()
    try:
        associate = db.get(Associate, associate_pk)
        if associate is None:
            return
        try:
            send_clerk_invite(db, associate, performed_by)
        except service.InvalidTransitionError as exc:   # suspended meanwhile, or already invited
            db.rollback()
            logger.info("Clerk invitation for %s not sent: %s", associate.associate_id, exc)
        generate_documents(db, associate, performed_by, issued_on)
    finally:
        db.close()


def territory_label(db: Session, associate: Associate) -> Optional[str]:
    parts = [
        db.get(model, pk).name
        for model, pk in ((TerritoryBlock, associate.territory_block_id),
                          (TerritoryDistrict, associate.territory_district_id),
                          (TerritoryState, associate.territory_state_id))
        if pk is not None
    ]
    return ", ".join(parts) or None


def card_details(db: Session, associate: Associate, photo: Optional[bytes], issued_on: date) -> documents.CardDetails:
    return documents.CardDetails(
        associate_id=associate.associate_id,
        full_name=associate.full_name,
        associate_type=associate.associate_type.value,
        level=associate.level.value if associate.level else None,
        territory=territory_label(db, associate),
        photo=photo,
        photo_content_type=associate.photo_content_type if photo else None,
        issued_on=issued_on,
    )


def generate_documents(db: Session, associate: Associate, performed_by: str, issued_on: date) -> Optional[str]:
    """Render and store the ID card and welcome letter. Returns None on success, or
    the recorded error. Never raises for a storage/render failure — the approval
    it follows already stands."""
    from app.storage import r2

    try:
        photo = r2.download_bytes(associate.photo_storage_key) if associate.photo_storage_key else None
        details = card_details(db, associate, photo, issued_on)
        card_pdf = documents.render_pdf(documents.id_card_html(details))
        letter_pdf = documents.render_pdf(documents.welcome_letter_html(details, texts.welcome_letter_body()))
        prefix = f"associates/{associate.associate_id}"
        card_key = f"{prefix}/id-card/{uuid.uuid4().hex}.pdf"
        letter_key = f"{prefix}/welcome-letter/{uuid.uuid4().hex}.pdf"
        r2.upload_bytes(card_key, card_pdf, "application/pdf")
        r2.upload_bytes(letter_key, letter_pdf, "application/pdf")
    except Exception as exc:  # recorded and surfaced; retried from the dashboard
        logger.warning("Associate documents failed for %s: %s", associate.associate_id, exc)
        error = f"{type(exc).__name__}: {exc}"[:500]
        service.record_documents(db, associate, performed_by, id_card_key=None, letter_key=None, error=error)
        db.commit()
        return error
    service.record_documents(db, associate, performed_by, id_card_key=card_key, letter_key=letter_key, error=None)
    db.commit()
    return None


def revoke_access(db: Session, associate: Associate, performed_by: str) -> dict:
    """After a suspension or deactivation committed (BE-19): Clerk bans the associate's
    user (every session revoked, sign-in refused) or, before they ever signed in, the
    invitation is revoked so its link stops working. The database already refuses every
    request from a non-active associate, so a Clerk failure leaves the change standing
    and is reported for a retry. Commits its own audit row."""
    from app.auth import clerk_client

    try:
        if associate.clerk_user_id:
            done = clerk_client.ban_user(associate.clerk_user_id)
            outcome = {"clerk_banned": done, "invitation_revoked": False, "clerk_error": None}
        elif associate.clerk_invite_status == ClerkInviteStatus.sent and associate.clerk_invitation_id:
            done = clerk_client.revoke_invitation(associate.clerk_invitation_id)
            if done:
                # Nothing is outstanding now: reinstating the associate means sending a new one.
                associate.clerk_invite_status = None
            outcome = {"clerk_banned": False, "invitation_revoked": done, "clerk_error": None}
        else:
            return {"clerk_banned": False, "invitation_revoked": False, "clerk_error": None}
    except clerk_client.ClerkSyncError as exc:
        logger.error("Clerk access not revoked for %s", associate.associate_id)
        return {"clerk_banned": False, "invitation_revoked": False, "clerk_error": str(exc)}
    if done:
        service.audit(db, "associate", associate.associate_id, "clerk_access_revoked", performed_by,
                      after={k: v for k, v in outcome.items() if k != "clerk_error"})
        db.commit()
    return outcome


def restore_access(db: Session, associate: Associate, performed_by: str) -> dict:
    """After a reinstatement committed: unban the Clerk user. (An associate who never
    signed in gets a new invitation from the dashboard's invite button.)"""
    from app.auth import clerk_client

    if not associate.clerk_user_id:
        return {"clerk_unbanned": False, "clerk_error": None}
    try:
        done = clerk_client.unban_user(associate.clerk_user_id)
    except clerk_client.ClerkSyncError as exc:
        logger.error("Clerk access not restored for %s", associate.associate_id)
        return {"clerk_unbanned": False, "clerk_error": str(exc)}
    if done:
        service.audit(db, "associate", associate.associate_id, "clerk_access_restored", performed_by,
                      after={"clerk_unbanned": True})
        db.commit()
    return {"clerk_unbanned": done, "clerk_error": None}
