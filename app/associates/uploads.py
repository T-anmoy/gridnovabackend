"""Registration document uploads: images or PDFs only, size-capped, typed by their
actual bytes rather than by the Content-Type the browser claims."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
DOCUMENT_TYPES = IMAGE_TYPES + ("application/pdf",)
# The passport-style photo is embedded in the ID card, so it must be an image.
ALLOWED_TYPES = {
    "masked_aadhaar": DOCUMENT_TYPES,
    "pan_card": DOCUMENT_TYPES,
    "photo": IMAGE_TYPES,
    "cancelled_cheque": DOCUMENT_TYPES,
}
_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}
_READ_CHUNK = 1024 * 1024


class UploadRejected(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Document:
    content: bytes
    content_type: str
    filename: str

    @property
    def extension(self) -> str:
        return _EXTENSIONS[self.content_type]


def sniff(head: bytes) -> Optional[str]:
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _safe_filename(name: Optional[str]) -> str:
    base = (name or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^\w.\- ]", "_", base).strip() or "upload"
    return base[:120]


async def read_document(upload, doc: str, allowed: Optional[tuple] = None,
                        max_bytes: Optional[int] = None) -> Document:
    """Read one uploaded file in chunks, stopping as soon as it passes the limit.
    `allowed` / `max_bytes` default to the registration document's own rules."""
    limit = MAX_DOCUMENT_BYTES if max_bytes is None else max_bytes
    chunks, size = [], 0
    while chunk := await upload.read(_READ_CHUNK):
        size += len(chunk)
        if size > limit:
            raise UploadRejected(f"{doc} exceeds the {limit // (1024 * 1024)}MB limit.", 413)
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise UploadRejected(f"{doc} is empty.")
    content_type = sniff(content[:16])
    allowed = ALLOWED_TYPES[doc] if allowed is None else allowed
    if content_type not in allowed:
        kinds = "a JPEG, PNG or WEBP image" if allowed == IMAGE_TYPES else "a JPEG, PNG or WEBP image, or a PDF"
        raise UploadRejected(f"{doc} must be {kinds}.")
    return Document(content=content, content_type=content_type, filename=_safe_filename(upload.filename))
