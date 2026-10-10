"""The two PDFs issued on approval: the ID card and the welcome letter.

Rendered with WeasyPrint, like proposal PDFs (app/pdf/generator.py). Building the
HTML is pure and separately testable; only render_pdf() needs WeasyPrint's native
libraries. The ID card's QR code is an inline SVG made by segno, pointing at the
public verification endpoint (GET /api/v1/public/verify/{associate_id}) on this
API's own public origin, PUBLIC_API_BASE_URL.
"""
from __future__ import annotations

import base64
import html
import io
from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.config import settings

TYPE_LABELS = {"associate": "Associate", "preferred_associate": "Preferred Associate"}
LEVEL_LABELS = {"state_head": "State Head", "district_head": "District Head", "block": "Block"}


class DocumentsNotConfiguredError(RuntimeError):
    """A setting the documents need is missing; retrying cannot fix it."""


@dataclass(frozen=True)
class CardDetails:
    associate_id: str
    full_name: str
    associate_type: str               # associate | preferred_associate
    level: Optional[str]              # state_head | district_head | block, preferred only
    territory: Optional[str]          # human-readable, preferred only
    photo: Optional[bytes]
    photo_content_type: Optional[str]
    issued_on: date


def verify_url(associate_id: str) -> str:
    base = settings.public_api_base_url.strip().rstrip("/")
    if not base:
        raise DocumentsNotConfiguredError(
            "PUBLIC_API_BASE_URL is not set — the ID card's QR code has no verification link to point at."
        )
    return f"{base}/api/v1/public/verify/{associate_id}"


def qr_svg(data: str) -> str:
    import segno

    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=4, border=1, xmldecl=False, svgns=True)
    return buf.getvalue().decode("utf-8")


def _company_name() -> str:
    from app.proposals.content_engine import load_content_config

    return str(load_content_config().get("company", {}).get("name") or "Grid Nova Energy")


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _photo_tag(details: CardDetails) -> str:
    if not details.photo or not details.photo_content_type:
        return '<div class="photo missing">No photo</div>'
    encoded = base64.b64encode(details.photo).decode()
    return f'<img class="photo" src="data:{_e(details.photo_content_type)};base64,{encoded}" alt="">'


def _role_lines(details: CardDetails) -> str:
    lines = [f'<div class="type">{_e(TYPE_LABELS.get(details.associate_type, details.associate_type))}</div>']
    if details.level:
        lines.append(f'<div class="level">{_e(LEVEL_LABELS.get(details.level, details.level))}</div>')
    if details.territory:
        lines.append(f'<div class="territory">{_e(details.territory)}</div>')
    return "".join(lines)


def id_card_html(details: CardDetails) -> str:
    qr = qr_svg(verify_url(details.associate_id))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><style>
  @page {{ size: 85.6mm 54mm; margin: 0; }}
  body {{ margin: 0; font-family: Arial, sans-serif; color: #111; }}
  .card {{ width: 85.6mm; height: 54mm; box-sizing: border-box; padding: 3mm 4mm; position: relative; }}
  .brand {{ font-size: 9pt; font-weight: bold; color: #0b3d2e; border-bottom: 0.6mm solid #f2a900; padding-bottom: 1mm; }}
  .body {{ display: flex; margin-top: 2.5mm; }}
  .photo {{ width: 20mm; height: 25mm; object-fit: cover; border: 0.3mm solid #999; }}
  .photo.missing {{ font-size: 6pt; color: #777; text-align: center; line-height: 25mm; }}
  .info {{ margin-left: 3mm; font-size: 7pt; line-height: 1.35; width: 36mm; }}
  .name {{ font-size: 9pt; font-weight: bold; }}
  .id {{ font-family: monospace; font-size: 8pt; margin-top: 1mm; }}
  .type {{ margin-top: 1mm; font-weight: bold; color: #0b3d2e; }}
  .qr {{ position: absolute; right: 3mm; bottom: 3mm; width: 17mm; height: 17mm; }}
  .qr svg {{ width: 17mm; height: 17mm; }}
  .foot {{ position: absolute; left: 4mm; bottom: 2mm; font-size: 5pt; color: #666; }}
</style></head><body><div class="card">
  <div class="brand">{_e(_company_name())}</div>
  <div class="body">
    {_photo_tag(details)}
    <div class="info">
      <div class="name">{_e(details.full_name)}</div>
      <div class="id">{_e(details.associate_id)}</div>
      {_role_lines(details)}
    </div>
  </div>
  <div class="qr">{qr}</div>
  <div class="foot">Issued {_e(details.issued_on.isoformat())} · Scan to verify</div>
</div></body></html>"""


def welcome_letter_html(details: CardDetails, body_text: str) -> str:
    territory = f"<p>Territory: {_e(details.territory)}</p>" if details.territory else ""
    level = (f"<p>Level: {_e(LEVEL_LABELS.get(details.level, details.level))}</p>"
             if details.level else "")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><style>
  @page {{ size: A4; margin: 25mm 22mm; }}
  body {{ font-family: Arial, sans-serif; color: #111; font-size: 11pt; line-height: 1.55; }}
  .brand {{ font-size: 20pt; font-weight: bold; color: #0b3d2e; border-bottom: 1mm solid #f2a900; padding-bottom: 3mm; }}
  .meta {{ margin: 8mm 0; }}
  .meta p {{ margin: 0; }}
  .body {{ white-space: pre-wrap; }}
</style></head><body>
  <div class="brand">{_e(_company_name())}</div>
  <div class="meta">
    <p>Date: {_e(details.issued_on.isoformat())}</p>
    <p>To: {_e(details.full_name)}</p>
    <p>Associate ID: {_e(details.associate_id)}</p>
    <p>Programme: {_e(TYPE_LABELS.get(details.associate_type, details.associate_type))}</p>
    {level}{territory}
  </div>
  <div class="body">{_e(body_text)}</div>
</body></html>"""


def render_pdf(html_doc: str) -> bytes:
    """The render choke point for ID cards, welcome letters, receipts and agreements —
    through the data-only URL fetcher (app/pdf/fetcher.py)."""
    from app.pdf.fetcher import render_pdf as render_with_safe_fetcher

    return render_with_safe_fetcher(html_doc)
