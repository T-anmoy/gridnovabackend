"""
Cloudflare R2 (S3-compatible) storage — the one real upload integration this
codebase has. Originally built inside app/pdf/generator.py for proposal PDFs;
extracted here so the proposal-inputs document/photo/signature upload path
(app/api/routes/proposal_inputs.py) reuses the exact same client and
config-check pattern instead of a second, independently-built one.
generator.py's own upload_to_r2() now delegates to upload_bytes() below.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# One S3 client per process (per credential set), built on first use. boto3's low-level
# clients are thread-safe, and building one per call cost a fresh connection pool and
# credential resolution every time. Bounded timeouts and few retries: a stalled R2
# request must fail inside the request's own time budget instead of boto3's defaults
# (60 s read timeout, legacy retries).
CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 2
_client_lock = threading.Lock()
_client = None
_client_identity: Optional[tuple] = None


class StorageNotConfiguredError(RuntimeError):
    """R2 credentials aren't set. Distinct from a transient upload failure —
    retrying can never fix a configuration gap, so callers should treat this
    as fatal on the first attempt rather than sleeping through a back-off for
    a failure mode that will be identical every time.
    """


def _s3():
    """The process-wide S3 client for the configured endpoint and credentials (rebuilt
    only when those settings change)."""
    global _client, _client_identity
    identity = (settings.storage_endpoint_url, settings.storage_access_key, settings.storage_secret_key)
    with _client_lock:
        if _client is None or _client_identity != identity:
            import boto3
            from botocore.config import Config

            _client = boto3.client(
                "s3",
                endpoint_url=settings.storage_endpoint_url,
                aws_access_key_id=settings.storage_access_key,
                aws_secret_access_key=settings.storage_secret_key,
                region_name="auto",
                config=Config(signature_version="s3v4", connect_timeout=CONNECT_TIMEOUT_SECONDS,
                              read_timeout=READ_TIMEOUT_SECONDS,
                              retries={"max_attempts": MAX_ATTEMPTS, "mode": "standard"}),
            )
            _client_identity = identity
        return _client


def reset_client() -> None:
    """Forget the cached client (tests that swap boto3 for a fake)."""
    global _client, _client_identity
    with _client_lock:
        _client, _client_identity = None, None


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

    _s3().put_object(Bucket=settings.storage_bucket, Key=key, Body=body, ContentType=content_type)
    return f"{_url_prefix()}{key}"


# ── Reading: the bucket is private ──────────────────────────────────────────────
# The URL upload_bytes() returns is R2's S3 API address, which rejects unsigned
# requests — it identifies an object, it is not a link anyone can open. Objects are
# read only through short-lived presigned GET URLs made here.

def _url_prefix() -> str:
    return f"{settings.storage_endpoint_url.rstrip('/')}/{settings.storage_bucket}/"


def key_from_url(url: Optional[str]) -> Optional[str]:
    """The object key inside a URL upload_bytes() returned, or None (logged) when the
    URL does not start with this deployment's {endpoint}/{bucket}/ prefix."""
    if not url:
        return None
    if settings.storage_endpoint_url and settings.storage_bucket and url.startswith(_url_prefix()):
        key = url[len(_url_prefix()):]
        if key:
            return key
    logger.warning("Stored object URL does not match the configured storage prefix; key not derived")
    return None


def signed_get_url(key: str, filename: Optional[str] = None, as_attachment: bool = False) -> tuple[str, datetime]:
    """A presigned GET URL for one object, valid for STORAGE_SIGNED_URL_TTL_SECONDS,
    and when it expires. Only ever a GET on a single named key — never list or write,
    and never any credential. Raises StorageNotConfiguredError when unconfigured."""
    if not is_configured():
        raise StorageNotConfiguredError("R2 storage is not configured — set STORAGE_* environment variables.")

    ttl = settings.storage_signed_url_ttl_seconds
    params = {"Bucket": settings.storage_bucket, "Key": key}
    if filename:
        disposition = "attachment" if as_attachment else "inline"
        # Quotes and control characters stripped: this value is echoed back as a header.
        safe = "".join(c for c in filename if c.isprintable() and c not in '"\\') or "download"
        params["ResponseContentDisposition"] = f'{disposition}; filename="{safe}"'

    url = _s3().generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)
    return url, datetime.now(timezone.utc) + timedelta(seconds=ttl)


def download_bytes(key: str) -> bytes:
    """Read one object's bytes server-side (e.g. an applicant's photo, to embed in
    their ID card). Raises StorageNotConfiguredError when unconfigured, and whatever
    boto3 raises for a missing object or a real failure."""
    if not is_configured():
        raise StorageNotConfiguredError("R2 storage is not configured — set STORAGE_* environment variables.")

    return _s3().get_object(Bucket=settings.storage_bucket, Key=key)["Body"].read()
