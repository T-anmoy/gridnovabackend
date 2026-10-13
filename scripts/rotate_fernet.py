"""Re-encrypt every stored secret with the newest FERNET_ENCRYPTION_KEY.

    python scripts/rotate_fernet.py            # report only
    python scripts/rotate_fernet.py --apply    # write

The rotation (deploy/RUNBOOK.md, "Rotating FERNET_ENCRYPTION_KEY"):
  1. FERNET_ENCRYPTION_KEY="<new>,<old>" (newest first) in Secret Manager; deploy.
  2. Run this with --apply (the migration job's image, same connection).
  3. FERNET_ENCRYPTION_KEY="<new>"; deploy. The old key can now be destroyed.

Covers employees.google_refresh_token and both associate application tables'
bank_account_encrypted and pan_encrypted (BE-4). Idempotent. A value no configured key can decrypt is reported
and left unchanged (it was already unreadable).
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


def rotate_all(db: Session) -> dict:
    from cryptography.fernet import InvalidToken

    from app.memory.models import AssociateApplication, Employee, PreferredAssociateApplication
    from app.security.encryption import rotate

    counts = {"rotated": 0, "unreadable": 0}
    targets = [(Employee, "google_refresh_token"), (AssociateApplication, "bank_account_encrypted"),
               (PreferredAssociateApplication, "bank_account_encrypted"),
               (AssociateApplication, "pan_encrypted"), (PreferredAssociateApplication, "pan_encrypted")]
    for model, column in targets:
        for row in db.execute(select(model).where(getattr(model, column).is_not(None))).scalars():
            try:
                setattr(row, column, rotate(getattr(row, column)))
                counts["rotated"] += 1
            except InvalidToken:
                counts["unreadable"] += 1
                print(f"rotate_fernet: {model.__tablename__} id={row.id}: no configured key decrypts it",
                      file=sys.stderr)
    db.flush()
    return counts


def main(database_url: str, apply: bool) -> int:
    print(f"rotate_fernet: target {make_url(database_url).render_as_string(hide_password=True)}")
    engine = create_engine(database_url)
    try:
        with Session(engine) as db:
            counts = rotate_all(db)
            db.commit() if apply else db.rollback()
        verb = "re-encrypted" if apply else "would re-encrypt (dry run; pass --apply)"
        print(f"rotate_fernet: {verb} {counts['rotated']} value(s); {counts['unreadable']} unreadable")
        return 1 if counts["unreadable"] else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main(settings.database_url, apply="--apply" in sys.argv[1:]))
