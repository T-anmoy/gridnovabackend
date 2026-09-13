"""
Cloudflare R2 (S3-compatible) storage — the one real upload integration this
codebase has. Originally built inside app/pdf/generator.py for proposal PDFs;
extracted here so the proposal-inputs document/photo/signature upload path
(app/api/routes/proposal_inputs.py) reuses the exact same client and
config-check pattern instead of a second, independently-built one.
generator.py's own upload_to_r2() now delegates to upload_bytes() below.
"""
from __future__ import annotations

from app.config import settings


class StorageNotConfiguredError(RuntimeError):
    """R2 credentials aren't set. Distinct from a transient upload failure —
    retrying can never fix a configuration gap, so callers should treat this
    as fatal on the first attempt rather than sleeping through a back-off for
    a failure mode that will be identical every time.
    """


def is_configured() -> bool:
    return all([
        settings.storage_access_key, settings.storage_secret_key,
        settings.storage_endpoint_url, settings.storage_bucket,
    ])


def upload_bytes(key: str, body: bytes, content_type: str) -> str:
    """Upload raw bytes to R2 under `key`. Returns the public URL. Raises
    StorageNotConfiguredError if the STORAGE_* settings are blank, and
    whatever boto3 itself raises for a real upload failure — neither is ever
    swallowed into a fake success.

    Config is checked BEFORE importing boto3 — a missing/broken boto3 install
    shouldn't masquerade as a slow "transient" failure a caller retries; an
    unconfigured deployment fails the same fast way either way.
    """
    if not is_configured():
        raise StorageNotConfiguredError("R2 storage is not configured — set STORAGE_* environment variables.")

    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.storage_endpoint_url,
        aws_access_key_id=settings.storage_access_key,
        aws_secret_access_key=settings.storage_secret_key,
    )
    s3.put_object(Bucket=settings.storage_bucket, Key=key, Body=body, ContentType=content_type)
    return f"{settings.storage_endpoint_url.rstrip('/')}/{settings.storage_bucket}/{key}"
