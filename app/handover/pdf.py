"""The handover pack PDF (English): customer, site, client code, system summary, the
installed units with their warranty end dates, the system warranty, O&M terms, the service
contact, dates and signatures. Rendered through app/associates/documents.render_pdf (the
data-only URL fetcher) with every value HTML-escaped; the acknowledgement signature is
embedded as a data: URI.

Nothing that is still a placeholder is printed: `held_reason` says whether the pack may be
rendered at all (the printed text itself still carries a dummy), and values Grid Nova has
not chosen (panel performance life, a category's default years) are left out, not guessed.
"""
from __future__ import annotations

import base64
import html
from typing import Optional

from sqlalchemy.orm import Session

from app.handover import config, warranty as warranty_register
from app.memory.models import (
    Customer, CustomerOnboarding, Equipment, HandoverPack, InstalledUnit, Project, ProposalInputs, WarrantyItem,
)
from app.pdf.fonts import FONT_STACK

HOLD_REGISTRY_ENTRIES = ("HW-OM",)
_e = lambda v: html.escape("" if v is None else str(v))   # noqa: E731


def company() -> dict:
    from app.execution import config as execution_config
    return execution_config.payments()["receipt"]["company"]


def service_contact() -> dict:
    """The configured service contact, else the company's main phone / email."""
    cfg = config.service()["contact"] or {}
    main = company()
    return {"phone": cfg.get("phone") or main["phone"], "email": cfg.get("email") or main["email"]}


def _printed_text() -> list[str]:
    main = company()
    contact = service_contact()
    return [main["legal_name"], main["address"], contact["phone"], contact["email"],
            config.warranty()["om_after_free_terms"]]


def hold_reason(db: Session) -> Optional[str]:
    """Why the pack must not be rendered / sent yet, or None (same rule as receipts)."""
    from app.config import settings
    from app.placeholders import registry

    if any(registry.MARKERS.search(str(text)) for text in _printed_text()):
        return "placeholder_content"
    if settings.is_production_like and registry.unresolved_ids(list(HOLD_REGISTRY_ENTRIES), db):
        return "placeholder_content"
    return None


def _signature_image(ref: Optional[dict]) -> Optional[str]:
    from app.storage import r2

    if not ref or not ref.get("storage_key"):
        return None
    try:
        content = r2.download_bytes(ref["storage_key"])
    except Exception:
        return None
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return "data:image/png;base64," + base64.b64encode(content).decode("ascii")


def _grid(headers: list[str], rows: list[list]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f'<table class="grid"><tr>{head}</tr>{body}</table>'


def _table(rows: list[tuple]) -> str:
    return "<table>" + "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows) + "</table>"


def build_html(db: Session, onboarding: CustomerOnboarding, pack: HandoverPack) -> str:
    from sqlalchemy import select

    from app.execution import register
    from app.timezones import format_ist_date

    customer = db.get(Customer, onboarding.customer_id)
    project = db.get(Project, onboarding.project_id)
    inputs = db.get(ProposalInputs, onboarding.proposal_inputs_id)
    warranty = warranty_register.ensure(db, onboarding, "system")
    data = warranty_register.serialize(db, warranty)
    units = {u.id: u for u in db.execute(select(InstalledUnit).where(InstalledUnit.project_id == onboarding.project_id))
             .scalars()}
    equipment = {e.id: e for e in db.execute(select(Equipment).where(
        Equipment.id.in_([u.equipment_id for u in units.values() if u.equipment_id]))).scalars()} if units else {}
    capacity = register.capacity(db, onboarding)
    package = register.project_package(db, onboarding)
    handover_on = format_ist_date_from_iso(data["handover_date"]) if data else ""
    system_rows = [("Client code", project.reference_id if project else ""), ("Customer", customer.name if customer else ""),
                   ("Site address", inputs.full_address if inputs else ""),
                   ("Installed capacity (modules)", f"{capacity['installed_module_kw']} kW"),
                   ("Connection phase", inputs.connection_phase.value.replace("_", " ") if inputs and inputs.connection_phase
                    else "—"),
                   ("Package", package.replace("_", " ").title() if package else "—"),
                   ("Handover date", handover_on)]
    unit_rows = []
    for item in (data or {}).get("units", []):
        unit = units.get(item["unit_id"])
        eq = equipment.get(unit.equipment_id) if unit and unit.equipment_id else None
        make = f"{eq.brand} {eq.model_name}" if eq else f"{unit.brand_as_printed or ''} {unit.model_as_printed or ''}".strip()
        end = format_ist_date_from_iso(item["warranty_end"]) if item["warranty_end"] else "—"
        unit_rows.append([item["category"].title(), make, item["serial_number"], end])
    warranty_rows = [("System warranty", f"{data['system']['years']} years from handover"),
                     ("Valid until", format_ist_date_from_iso(data["system"]["ends"]))] if data else []
    if data and data["panel_performance_life_years"]:
        warranty_rows.append(("Expected panel performance life", f"{data['panel_performance_life_years']} years "
                                                                  "(a life span, not a warranty)"))
    om_rows = []
    if data:
        om_rows = [("First-year O&M", f"Free for {data['om']['free_months']} months, until "
                                      f"{format_ist_date_from_iso(data['om']['free_until'])}"),
                   ("After the free period", config.warranty()["om_after_free_terms"])]
    contact = service_contact()
    sla = config.service()["sla_hours"]
    contact_rows = [("Phone", contact["phone"]), ("Email", contact["email"])]
    if sla:
        contact_rows.append(("Service target", f"We aim to resolve a service request within {sla} hours."))
    main = company()
    ack = "Acknowledged in person" if pack.ack_method == "in_person" else "Signed on screen"
    image = _signature_image(pack.ack_signature_ref)
    sig = (f'<img class="sig" src="{_e(image)}">' if image else "")
    sign_rows = [("Orientation given", format_ist_date(pack.orientation_done_at) if pack.orientation_done_at else "—"),
                 ("O&M contact details given", format_ist_date(pack.om_contact_given_at) if pack.om_contact_given_at
                  else "—"),
                 ("Customer acknowledgement", f"{ack} — {pack.ack_customer_name} ({format_ist_date(pack.acknowledged_at)})"
                  if pack.acknowledged_at else "—")]
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font-family: {FONT_STACK}; font-size: 10.5pt; margin: 18mm; color: #111; }}
h1 {{ font-size: 17pt; margin: 0 0 2mm; }} h2 {{ font-size: 12pt; margin: 7mm 0 2mm; border-bottom: 1px solid #bbb; }}
th {{ text-align: left; padding: 1.2mm 6mm 1.2mm 0; vertical-align: top; width: 52mm; }}
table.grid {{ width: 100%; border-collapse: collapse; }} table.grid th, table.grid td {{ border: 1px solid #ccc;
  padding: 1.5mm 2mm; width: auto; }} .company {{ margin-bottom: 5mm; }} .sig {{ max-height: 28mm; display: block; }}
</style></head><body>
<div class="company"><strong>{_e(main["legal_name"])}</strong><br>{_e(main["address"])}<br>
{_e(contact["phone"])} · {_e(contact["email"])}</div>
<h1>Handover pack and warranty certificate</h1>
<h2>System</h2>{_table(system_rows)}
<h2>Installed equipment</h2>{_grid(["Type", "Make and model", "Serial number", "Product warranty until"], unit_rows)}
<h2>Warranty</h2>{_table(warranty_rows)}
<h2>Operations and maintenance</h2>{_table(om_rows)}
<h2>Service contact</h2>{_table(contact_rows)}
<h2>Handover and signatures</h2>{_table(sign_rows)}{sig}
</body></html>"""


def format_ist_date_from_iso(value: str) -> str:
    from datetime import date
    return date.fromisoformat(value).strftime("%d %B %Y")
