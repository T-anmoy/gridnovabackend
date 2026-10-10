"""Uploading one execution file (photo or document) — shared by the associate route and
Grid Nova's own staff route. Private storage; typed by the bytes, not the claimed
Content-Type; photos carry their provenance (app/execution/photos.py)."""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.associates import uploads
from app.associates.service import AssociateError
from app.execution import config, photos, service
from app.memory.models import CustomerOnboarding, ProjectExecution

# Per-file limit; app/security/upload_limit.py reads it at call time for the body bound.
MAX_FILE_BYTES = 10 * 1024 * 1024


def store(db: Session, onboarding: CustomerOnboarding, execution: ProjectExecution, upload, *, kind: str,
          name: Optional[str], uploaded_by: str, latitude: Optional[float] = None,
          longitude: Optional[float] = None, captured_at: Optional[str] = None,
          capture_method: Optional[str] = None, exif_taken_at_client: Optional[str] = None) -> dict:
    """Raises AssociateError (422/503) or uploads.UploadRejected (413/422)."""
    from app.storage import r2

    if (latitude is None) != (longitude is None):
        raise AssociateError("latitude and longitude must be given together.", code="gps_pair", status_code=422,
                             field="latitude")
    if kind == "photo":
        if name not in config.photo_slots():
            raise AssociateError(f"photo_slot must be one of {config.photo_slots()}.", code="unknown_photo_slot",
                                 status_code=422, field="photo_slot")
        allowed = uploads.IMAGE_TYPES
    elif kind == "document":
        if name not in config.document_types():
            raise AssociateError(f"document_type must be one of {config.document_types()}.",
                                 code="unknown_document_type", status_code=422, field="document_type")
        allowed = uploads.DOCUMENT_TYPES
    else:
        raise AssociateError("kind must be photo or document.", code="invalid_kind", status_code=422, field="kind")
    document = uploads.read_document_sync(upload, name, allowed=allowed, max_bytes=MAX_FILE_BYTES)
    extra: dict = {}
    if kind == "photo":
        extra = photos.provenance(content=document.content, capture_method=capture_method, latitude=latitude,
                                  longitude=longitude, captured_at=captured_at,
                                  exif_taken_at_client=exif_taken_at_client)
    if latitude is not None:
        extra.update(latitude=latitude, longitude=longitude)
    if captured_at:
        extra["client_captured_at"] = captured_at
    key = f"executions/{onboarding.project_id}/{kind}/{uuid.uuid4().hex}_{document.filename}"
    try:
        r2.upload_bytes(key, document.content, document.content_type)
    except r2.StorageNotConfiguredError as exc:
        raise AssociateError(str(exc), code="storage_not_configured", status_code=503)
    if kind == "photo":
        photos.flag_duplicate_and_record(db, extra, project_id=onboarding.project_id, source="execution_photo",
                                         slot=name, storage_key=key)
    return service.attach_file(db, execution, kind=kind, name=name, storage_key=key, filename=document.filename,
                               content_type=document.content_type, uploaded_by=uploaded_by, extra=extra)
