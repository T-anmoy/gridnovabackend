"""Re-sign every associate identity claim with the current IDENTITY_HMAC_KEY.

    python scripts/rehash_identity_claims.py            # report only
    python scripts/rehash_identity_claims.py --apply    # write

Run once when IDENTITY_HMAC_KEY is first set on a database whose claims were signed
with the old key (before BE-3 the claim key was derived from FERNET_ENCRYPTION_KEY), and
again whenever IDENTITY_HMAC_KEY itself is changed. A fresh staging or production
database has no old claims and never needs it.

Each claim is recomputed from its application's stored data — PAN and bank account
(decrypted with FERNET_ENCRYPTION_KEY through the audited helper app/security/pii.py),
mobile, and Aadhaar last 4 + date of birth + name — exactly as app/associates/service.identity_claims() signs a new registration, so
the result does not depend on the old key at all. Idempotent: a second run changes
nothing. Until it has run, duplicate detection does not recognise the old claims.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402


ACTOR = "script:rehash_identity_claims"


def rehash(db: Session) -> dict:
    from app.associates import service, validation
    from app.memory.models import AssociateIdentityClaim
    from app.security import pii

    key = service.identity_claim_key()
    counts = {"claims": 0, "changed": 0, "missing_application": 0}
    rows = list(db.execute(select(AssociateIdentityClaim).order_by(AssociateIdentityClaim.id)).scalars())
    expected: dict[tuple, dict[str, str]] = {}
    for claim in rows:
        counts["claims"] += 1
        owner = (claim.application_type, claim.application_id)
        if owner not in expected:
            app = service.get_application(db, *owner)
            expected[owner] = {} if app is None else service.identity_claims(
                pan=pii.reveal(db, app, "pan", actor=ACTOR, purpose="identity_claim_rehash"), mobile=app.mobile,
                ifsc=app.bank_ifsc,
                account=pii.reveal(db, app, "bank_account", actor=ACTOR, purpose="identity_claim_rehash"),
                aadhaar_last4=app.aadhaar_last4, dob=app.date_of_birth,
                name_key=validation.name_key(app.full_name), key=key)
        if not expected[owner]:
            counts["missing_application"] += 1
            continue
        if claim.value_hash != expected[owner][claim.kind]:
            claim.value_hash = expected[owner][claim.kind]
            counts["changed"] += 1
    db.flush()
    return counts


def main(database_url: str, apply: bool) -> int:
    if not settings.identity_hmac_key:
        print("rehash_identity_claims: REFUSED — IDENTITY_HMAC_KEY is not set.", file=sys.stderr)
        return 2
    print(f"rehash_identity_claims: target {make_url(database_url).render_as_string(hide_password=True)}")
    engine = create_engine(database_url)
    try:
        with Session(engine) as db:
            counts = rehash(db)
            if apply:
                db.commit()
            else:
                db.rollback()
        verb = "updated" if apply else "would update (dry run; pass --apply)"
        print(f"rehash_identity_claims: {counts['claims']} claims, {verb} {counts['changed']}, "
              f"{counts['missing_application']} without an application (left as they are)")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main(settings.database_url, apply="--apply" in sys.argv[1:]))
