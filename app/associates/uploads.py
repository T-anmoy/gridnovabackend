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
    def __init__(self, message: str, status_code: int = 422, code: str = "invalid_file"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


# iPhone photos (HEIC/HEIF) are not accepted: the app converts them to JPEG before
# uploading. They are recognised so the refusal says so, rather than "not an image".
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")


def is_heif(head: bytes) -> bool:
    return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS


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


def _checked(content: bytes, doc: str, allowed: Optional[tuple], filename: Optional[str]) -> Document:
    if not content:
        raise UploadRejected(f"{doc} is empty.")
    content_type = sniff(content[:16])
    allowed = ALLOWED_TYPES[doc] if allowed is None else allowed
    if content_type is None and is_heif(content[:16]):
        raise UploadRejected(f"{doc} is an iPhone HEIC/HEIF photo — convert it to JPEG before uploading.",
                             code="heic_not_supported")
    if content_type not in allowed:
        kinds = "a JPEG, PNG or WEBP image" if allowed == IMAGE_TYPES else "a JPEG, PNG or WEBP image, or a PDF"
        raise UploadRejected(f"{doc} must be {kinds}.")
    return Document(content=content, content_type=content_type, filename=_safe_filename(filename))


def _too_big(doc: str, limit: int) -> UploadRejected:
    return UploadRejected(f"{doc} exceeds the {limit // (1024 * 1024)}MB limit.", 413)


async def read_document(upload, doc: str, allowed: Optional[tuple] = None,
                        max_bytes: Optional[int] = None) -> Document:
    """Read one uploaded file in chunks, stopping as soon as it passes the limit.
    `allowed` / `max_bytes` default to the registration document's own rules. For a
    route that must await the request itself (request.form())."""
    limit = MAX_DOCUMENT_BYTES if max_bytes is None else max_bytes
    chunks, size = [], 0
    while chunk := await upload.read(_READ_CHUNK):
        size += len(chunk)
        if size > limit:
            raise _too_big(doc, limit)
        chunks.append(chunk)
    return _checked(b"".join(chunks), doc, allowed, upload.filename)


def read_document_sync(upload, doc: str, allowed: Optional[tuple] = None,
                       max_bytes: Optional[int] = None) -> Document:
    """The same, for plain (threadpool) route functions: FastAPI has already spooled
    the upload to `upload.file` before the route runs, so it is read synchronously,
    off the event loop."""
    limit = MAX_DOCUMENT_BYTES if max_bytes is None else max_bytes
    upload.file.seek(0)
    chunks, size = [], 0
    while chunk := upload.file.read(_READ_CHUNK):
        size += len(chunk)
        if size > limit:
            raise _too_big(doc, limit)
        chunks.append(chunk)
    return _checked(b"".join(chunks), doc, allowed, upload.filename)
