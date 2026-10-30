"""The structured proposal document (BE-13): everything that varies on the customer PDF,
stored with the proposal version in `Proposal.content["document"]`.

    build()            drafts it from what the system already holds — the lead, the
                       customer, the site assessment / survey (ProposalInputs), the
                       associate onboarding and Grid Nova's price list — so a typical
                       up-to-10 kW PM Surya Ghar proposal needs no typing.
    figures()          THE calculation: GST, totals, amounts in words, payment amounts,
                       subsidy and effective cost, generation, roof area. The PDF, the
                       dashboard and the accept page all read it; nothing is typed.
    missing_required() what still stops approval and sending ({field, message} list).
    apply_edit()       one section replaced by staff, validated, diffed field by field.

Fixed text, the header / footer / watermark / letterhead and the signatory live in
config/proposal_document.yaml and never in the document: staff cannot change them.
Amounts are Decimal and travel as decimal strings.
"""
from __future__ import annotations

import copy
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.proposals import document_config

SCHEMA_VERSION = 1
PAISA = Decimal("0.01")
GST_RATE = Decimal("0.089")
MAX_KWP = Decimal("100000")
PRICE_MODES = ("price_list", "staff_basic", "staff_total", "none")
CATEGORIES = ("Residential", "Commercial", "Industrial", "Government", "Agricultural")
COVERED_SEGMENTS = ("residential", "commercial", "industrial")
DEFAULTED_SECTIONS = ("boq", "customer_scope", "payment_terms")
_MARKERS = re.compile(r"\[PLACEHOLDER|\[UNCONFIRMED|DUMMY")
_MOBILE_RE = re.compile(r"^[6-9]\d{9}$")


class DocumentError(ValueError):
    """A structured 4xx: {"code", "message", "field"?}."""

    def __init__(self, code: str, message: str, field: Optional[str] = None, status_code: int = 422,
                 extra: Optional[dict] = None):
        super().__init__(message)
        self.code, self.message, self.field, self.status_code = code, message, field, status_code
        self.extra = extra or {}

    def body(self) -> dict:
        out = {"code": self.code, "message": self.message, **self.extra}
        if self.field:
            out["field"] = self.field
        return out


# ── The model ─────────────────────────────────────────────────────────────────────

def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None


def _text_ok(value: str, where: str) -> str:
    """Text staff type: no draft markers, and only the tokens that follow the plant."""
    if _MARKERS.search(value):
        raise ValueError(f"{where} may not contain placeholder markers")
    unknown = document_config.tokens_in(value) - document_config.EDITABLE_TOKENS
    if unknown:
        raise ValueError(f"{where} uses unknown placeholder(s) {sorted(unknown)}; allowed: "
                         f"{sorted(document_config.EDITABLE_TOKENS)}")
    return value


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _address_lines(v: list[str]) -> list[str]:
    out = [_clean(line) for line in v if _clean(line)]
    for line in out:
        if len(line) > 120:
            raise ValueError("an address line may be at most 120 characters")
        _text_ok(line, "an address line")
    return out


class Addressee(_Section):
    customer_type: Literal["individual", "organisation"] = "individual"
    title: Optional[str] = None
    name: Optional[str] = Field(default=None, max_length=120)
    attention: Optional[str] = Field(default=None, max_length=120)
    address_lines: list[str] = Field(default_factory=list, max_length=5)
    mobile: Optional[str] = None
    aadhaar_last4: Optional[str] = None
    salutation: str = ""

    @field_validator("name", "attention")
    @classmethod
    def _plain(cls, v):
        return _text_ok(_clean(v), "the name") if v is not None and _clean(v) else None

    @field_validator("address_lines")
    @classmethod
    def _lines(cls, v):
        return _address_lines(v)

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        if v in (None, ""):
            return None
        if v not in document_config.load()["titles"]:
            raise ValueError(f"title must be one of {document_config.load()['titles']}")
        return v

    @field_validator("salutation")
    @classmethod
    def _salutation(cls, v):
        options = document_config.load()["salutations"]
        if not v:
            return document_config.load()["default_salutation"]
        if v not in options:
            raise ValueError(f"salutation must be one of {options}")
        return v

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, v):
        if v in (None, ""):
            return None
        digits = re.sub(r"\D", "", v)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if not _MOBILE_RE.match(digits):
            raise ValueError("mobile must be a 10-digit Indian mobile number")
        return digits

    @field_validator("aadhaar_last4")
    @classmethod
    def _aadhaar(cls, v):
        # UIDAI masking: only the last four digits are ever held or printed.
        if v in (None, ""):
            return None
        if not re.fullmatch(r"\d{4}", v):
            raise ValueError("aadhaar_last4 must be exactly the last 4 digits (the full number is never stored)")
        return v


class Connection(_Section):
    discom: Optional[str] = Field(default=None, max_length=60)
    consumer_number: Optional[str] = Field(default=None, max_length=40)
    sub_division: Optional[str] = Field(default=None, max_length=80)
    sanctioned_load_kw: Optional[Decimal] = Field(default=None, gt=0, le=MAX_KWP)
    category: Optional[Literal[CATEGORIES]] = None
    phase: Optional[Literal["single", "three"]] = None

    _kw = field_validator("sanctioned_load_kw")(lambda cls, v: _plain_kw(v))

    @field_validator("discom", "consumer_number", "sub_division")
    @classmethod
    def _plain(cls, v):
        return _text_ok(_clean(v), "connection text") if v is not None and _clean(v) else None


class Site(_Section):
    address_lines: list[str] = Field(default_factory=list, max_length=5)
    town: Optional[str] = Field(default=None, max_length=80)
    state: Optional[str] = Field(default=None, max_length=60)

    @field_validator("address_lines")
    @classmethod
    def _lines(cls, v):
        return _address_lines(v)

    @field_validator("town", "state")
    @classmethod
    def _plain(cls, v):
        return _text_ok(_clean(v), "site text") if v is not None and _clean(v) else None


def _plain_kw(v: Optional[Decimal]) -> Optional[Decimal]:
    """3.0 → 3, 3.50 → 3.5 (never an exponent), so stored values read as typed."""
    return None if v is None else Decimal(kw_text(v))


class PmSuryaGhar(_Section):
    subsidy_opted: bool = False
    portal_registered: bool = False
    approved_capacity_kwp: Optional[Decimal] = Field(default=None, gt=0, le=MAX_KWP)

    _kw = field_validator("approved_capacity_kwp")(lambda cls, v: _plain_kw(v))


class Plant(_Section):
    capacity_kwp: Optional[Decimal] = Field(default=None, gt=0, le=MAX_KWP, decimal_places=3)
    package: Optional[Literal["NOVA_ASSURED", "NOVA_SIGNATURE"]] = None
    dcr: bool = False

    _kw = field_validator("capacity_kwp")(lambda cls, v: _plain_kw(v))


class System(_Section):
    module_technology: str = "Mono PERC"
    inverter: Optional[str] = Field(default=None, max_length=160)
    mounting: Optional[str] = Field(default=None, max_length=160)

    @field_validator("module_technology")
    @classmethod
    def _technology(cls, v):
        allowed = technologies()
        if v not in allowed:
            raise ValueError(f"module_technology must be one of {allowed} (the price list's options)")
        return v

    @field_validator("inverter", "mounting")
    @classmethod
    def _plain(cls, v):
        return _text_ok(_clean(v), "system text") if v is not None and _clean(v) else None


class BoqRow(_Section):
    description: str = Field(min_length=1, max_length=160)
    quantity: str = Field(min_length=1, max_length=60)
    standard: Optional[str] = Field(default=None, max_length=60)
    make: str = Field(min_length=1, max_length=160)
    kind: Optional[Literal["module", "inverter", "cable"]] = None

    @field_validator("description", "quantity", "standard", "make")
    @classmethod
    def _plain(cls, v):
        return _text_ok(_clean(v), "a BOQ cell") if v is not None and _clean(v) else v


class PaymentTerm(_Section):
    percent: Decimal = Field(gt=0, le=100, decimal_places=2)
    label: str = Field(min_length=1, max_length=120)
    short_label: Optional[str] = Field(default=None, max_length=80)

    @field_validator("label", "short_label")
    @classmethod
    def _plain(cls, v):
        return _text_ok(_clean(v), "a payment label") if v is not None and _clean(v) else None


class Price(_Section):
    mode: Literal[PRICE_MODES] = "none"
    basic: Optional[Decimal] = Field(default=None, gt=0)
    rate_per_kwp: Optional[Decimal] = Field(default=None, gt=0)
    total: Optional[Decimal] = Field(default=None, gt=0)
    price_list_version: Optional[int] = None


def _bullets(values: list[str], where: str) -> list[str]:
    out = []
    for value in values:
        value = _clean(value)
        if not value:
            continue
        if len(value) > 400:
            raise ValueError(f"{where}: each item may be at most 400 characters")
        out.append(_text_ok(value, where))
    return out


class ProposalDocument(BaseModel):
    """Version 1 of the structured document. `revision` increments on every saved edit
    (optimistic concurrency); `edited` lists the sections staff have changed: those are
    never regenerated from defaults and are carried into a revision."""
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    revision: int = 0
    segment: Optional[str] = None
    coverage: Optional[str] = None            # None = a covered segment; else the uncovered one
    housing_society: bool = False
    mount_type: Literal["rooftop", "ground_mounted"] = "rooftop"
    addressee: Addressee = Field(default_factory=Addressee)
    connection: Connection = Field(default_factory=Connection)
    site: Site = Field(default_factory=Site)
    pm_surya_ghar: PmSuryaGhar = Field(default_factory=PmSuryaGhar)
    plant: Plant = Field(default_factory=Plant)
    system: System = Field(default_factory=System)
    boq: list[BoqRow] = Field(default_factory=list, max_length=30)
    customer_scope: list[str] = Field(default_factory=list, max_length=20)
    payment_terms: list[PaymentTerm] = Field(default_factory=list, max_length=6)
    notes: list[str] = Field(default_factory=list, max_length=10)
    price: Price = Field(default_factory=Price)
    defaults: dict[str, str] = Field(default_factory=dict)
    edited: list[str] = Field(default_factory=list)      # sections staff changed

    @field_validator("customer_scope")
    @classmethod
    def _scope(cls, v):
        return _bullets(v, "customer_scope")

    @field_validator("notes")
    @classmethod
    def _notes(cls, v):
        return _bullets(v, "notes")

    @model_validator(mode="after")
    def _terms_total(self):
        if self.payment_terms and sum(t.percent for t in self.payment_terms) != 100:
            raise ValueError("payment_terms must total 100%")
        return self


def technologies() -> list[str]:
    from app.pricing import price_list
    found: list[str] = []
    for package in price_list.PACKAGES:
        for tech in price_list.equipment(package)["solar_panel"].get("technology") or []:
            if tech not in found:
                found.append(tech)
    return found


def parse(raw: dict) -> ProposalDocument:
    return ProposalDocument.model_validate(raw)


def dump(doc: ProposalDocument) -> dict:
    return doc.model_dump(mode="json")


def stored(content: Optional[dict]) -> Optional[ProposalDocument]:
    """The proposal's document, or None for a proposal drafted before BE-13. A lead
    converted from the Proposal Studio carries a schema-2 document."""
    raw = (content or {}).get("document")
    return parse_any(raw) if isinstance(raw, dict) else None


# ── Numbers ──────────────────────────────────────────────────────────────────────

def money(value) -> Decimal:
    return Decimal(str(value)).quantize(PAISA, rounding=ROUND_HALF_UP)


def kw_text(value) -> str:
    """3 → "3", 3.50 → "3.5", 52 → "52"."""
    if value in (None, ""):
        return ""
    d = Decimal(str(value)).normalize()
    return f"{d:f}" if d != d.to_integral_value() else str(int(d))


def grouped(amount, whole: bool) -> str:
    """Indian digit grouping: 2,01,465 (whole-rupee documents) or 2,01,465.50."""
    from app.pricing.price_list import indian_grouping
    text = indian_grouping(amount)
    return text[:-3] if whole and text.endswith(".00") else text


def _round_to(value: Decimal, step: int) -> int:
    return int((value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step)


def layout(doc: ProposalDocument) -> str:
    if isinstance(doc, StudioDocument):
        rule = studio_project(doc)["layout"]
        if rule != "compact_or_detailed":
            return rule
    return document_config.layout_for(doc.plant.capacity_kwp)


def subsidy_applies(doc: ProposalDocument) -> bool:
    """PM Surya Ghar CFA: residential (not a housing society), subsidy opted, DCR modules."""
    return (doc.segment == "residential" and not doc.housing_society and doc.pm_surya_ghar.subsidy_opted
            and doc.plant.dcr and doc.plant.capacity_kwp is not None)


def basic_before_discount(price: Price, capacity) -> Optional[Decimal]:
    """The basic price the price mode gives, before any Studio discount."""
    basic = price.basic
    if price.mode == "staff_basic" and price.rate_per_kwp and capacity:
        basic = money(Decimal(str(price.rate_per_kwp)) * Decimal(str(capacity)))
    return money(basic) if basic else None


def price_parts(price: Price, capacity) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """(basic, gst, total). GST is 8.9% of the basic price (5% on 70% + 18% on 30%) and
    the total is basic + GST — except a total staff set as a whole (adjust-pricing's
    `new_value`), which stays exactly as set: basic = total / 1.089, GST = the rest.
    A Studio discount (schema 2) comes off the basic price before GST."""
    from app.pricing.price_list import gst_of
    if price.mode == "staff_total" and price.total:
        total = money(price.total)
        basic = money(total / (1 + GST_RATE))
        return basic, total - basic, total
    basic = basic_before_discount(price, capacity)
    discount = getattr(price, "discount", None)
    if basic is not None and discount is not None:
        basic = money(basic - discount.amount)
    if not basic or basic <= 0:
        return None, None, None
    gst = gst_of(basic)
    return basic, gst, basic + gst


def figures(doc: ProposalDocument) -> dict:
    """Every computed figure on the document, as decimal strings where money. One
    function: the PDF, the dashboard, the accept page and the Proposal Studio all read
    this. A schema-2 (Studio) document adds its sections' figures (studio_figures)."""
    if isinstance(doc, StudioDocument):
        return studio_figures(doc)
    return _plant_figures(doc)


def _plant_figures(doc: ProposalDocument) -> dict:
    from app.estimate import engine
    from app.estimate import subsidy as pm_surya_ghar
    from app.estimate.config import estimate_config, subsidy_config
    from app.pricing.amount_words import amount_in_words
    from app.rules import sales_policy

    cfg = document_config.load()
    cap = doc.plant.capacity_kwp
    basic, gst, total = price_parts(doc.price, cap)
    payments = []
    if total is not None and doc.payment_terms:
        running = Decimal(0)
        for i, term in enumerate(doc.payment_terms):
            last = i == len(doc.payment_terms) - 1
            amount = total - running if last else money(total * term.percent / 100)
            running += amount
            payments.append({"percent": kw_text(term.percent), "label": term.label, "short_label": term.short_label,
                             "amount": str(amount)})
    # The solar estimate engine (app/estimate/): the central CFA slabs for the site's state
    # (special-category rates where they apply), generation and roof area — the same
    # figures as the calculator. A confirmed state top-up is printed only when
    # config/subsidies.yaml proposal.print_confirmed_state_top_up is on; announced ones never.
    state = doc.site.state
    applies = subsidy_applies(doc)
    slabs, state_lines, subsidy_total = [], [], Decimal(0)
    if applies:
        capacity, _basis, _note = pm_surya_ghar.cfa_capacity(Decimal(str(cap)), None)
        rows, central = pm_surya_ghar.residential_slabs(capacity, state)
        slabs = [(r["kwp"], r["rate_per_kwp"], r["amount"]) for r in rows]
        subsidy_total = money(central)
        if subsidy_config()["proposal"]["print_confirmed_state_top_up"]:
            state_lines = [t for t in pm_surya_ghar.state_top_ups(state, Decimal(str(cap)), engine.today_ist())
                           if t["status"] == "confirmed"]
            subsidy_total += sum((t["amount"] for t in state_lines), Decimal(0))
    effective = total - subsidy_total if (applies and total is not None) else None
    per_kwp = int(engine.generation_rate(state).value)
    monthly = int(engine.monthly_generation(Decimal(str(cap)), state)) if cap else None
    roof = estimate_config()["roof_area"]
    space = {"sqft_per_kwp_low": roof["sqft_per_kwp_low"].value, "sqft_per_kwp_high": roof["sqft_per_kwp"].value}
    amounts = [a for a in (basic, gst, total, subsidy_total, effective) if a is not None]
    amounts += [Decimal(p["amount"]) for p in payments]
    whole = all(a == a.to_integral_value() for a in amounts)
    return {
        "layout": layout(doc),
        "capacity_kwp": kw_text(cap) if cap else None,
        "price_mode": doc.price.mode,
        "basic": str(basic) if basic is not None else None,
        "gst": str(gst) if gst is not None else None,
        "total": str(total) if total is not None else None,
        "gst_rate": str(GST_RATE),
        "words": {"basic": amount_in_words(basic) if basic is not None else None,
                  "gst": amount_in_words(gst) if gst is not None else None,
                  "total": amount_in_words(total) if total is not None else None},
        "payments": payments,
        "whole_rupees": whole,
        "subsidy": {"applies": applies,
                    "slabs": [{"kw": kw_text(Decimal(str(k))), "rate": str(money(r)), "amount": str(money(a))}
                              for k, r, a in slabs],
                    "state_lines": [{"state": t["state"], "amount": str(t["amount"])} for t in state_lines],
                    "total": str(subsidy_total) if applies else None,
                    "effective_cost": str(effective) if effective is not None else None},
        "generation": {"units_per_kwp_per_month": per_kwp,
                       "monthly_units": monthly, "annual_units": monthly * 12 if monthly is not None else None},
        "space_required_sqft": ({"low": _round_to(Decimal(str(cap)) * space["sqft_per_kwp_low"], 10),
                                 "high": _round_to(Decimal(str(cap)) * space["sqft_per_kwp_high"], 10),
                                 "single": _round_to(Decimal(str(cap)) * space["sqft_per_kwp_low"], 100)}
                                if cap else None),
        "validity_days": sales_policy.proposal_validity_days(),
    }


# ── What stops approval ─────────────────────────────────────────────────────────

def missing_required(doc: ProposalDocument, figs: Optional[dict] = None) -> list[dict]:
    """[{field, message}] — empty when the document may be approved and sent. Nothing
    printed on a customer PDF may be missing, a placeholder or 'None'."""
    figs = figs or figures(doc)
    out = []

    def need(cond, field: str, message: str) -> None:
        if not cond:
            out.append({"field": field, "message": message})

    a = doc.addressee
    need(a.name, "addressee.name", "the customer's name" if a.customer_type == "individual"
         else "the organisation's name")
    need(a.address_lines, "addressee.address_lines", "the customer's address")
    need(doc.plant.capacity_kwp, "plant.capacity_kwp", "the plant capacity (kWp)")
    need(doc.connection.phase, "connection.phase", "the electrical supply (single or three phase)")
    need(figs["total"] is not None, "price", "a price (the price list, or adjust-pricing)")
    need(doc.boq, "boq", "at least one Bill of Quantity row")
    need(doc.customer_scope, "customer_scope", "at least one customer-scope item")
    need(doc.payment_terms, "payment_terms", "the payment terms")
    need(doc.site.town, "site.town", "the town named in the letter")
    need(doc.site.state, "site.state", "the site's state (Destination)")
    need(doc.connection.category, "connection.category", "the consumer category")
    if figs["layout"] == "compact":
        need(doc.connection.discom, "connection.discom", "the DISCOM")
        need(doc.connection.consumer_number, "connection.consumer_number", "the consumer number")
    else:
        need(doc.site.address_lines, "site.address_lines", "the project site address")
    if doc.pm_surya_ghar.portal_registered:
        need(doc.pm_surya_ghar.approved_capacity_kwp, "pm_surya_ghar.approved_capacity_kwp",
             "the capacity approved on the PM Surya Ghar portal")
    need(doc.mount_type == "rooftop", "plant.mount_type",
         "a rooftop plant (the proposal templates describe rooftop plants only)")
    if doc.coverage is not None:
        keys = document_config.load()["uncovered_segments"].get(doc.coverage, [])
        if any(_open_section(k) for k in keys):
            out.append({"field": "segment_text",
                        "message": f"Grid Nova's text for {doc.coverage} proposals (a placeholder, or not yet "
                                   f"approved in the dashboard: {', '.join(keys)})"})
    return out


def _open_section(key: str) -> bool:
    """The segment's text is still missing: absent, a placeholder, or (BE-22) wording an
    Admin has not approved exactly as it stands."""
    from app.proposals import content_engine
    section_id = key.split(".", 1)[1]
    value = (content_engine.load_content_config().get("sections_library") or {}).get(section_id)
    if value is None or bool(_MARKERS.search(str(value))):
        return True
    entry_id = content_engine.section_entry(section_id)
    if entry_id is None:
        return False
    from app.placeholders import registry
    return not registry.text_is_approved(entry_id, text=str(value))


# ── Building it ──────────────────────────────────────────────────────────────────

def _val(v):
    return v.value if hasattr(v, "value") else v


def _segment_and_coverage(collected: dict, inputs) -> tuple[Optional[str], Optional[str], str]:
    from app.proposals.price_list_content import SEGMENT_FROM_SITE_TYPE
    site_type = _val(inputs.site_type) if inputs is not None else None
    scheme = _val(inputs.scheme_name) if inputs is not None else None
    project_type = collected.get("project_type")
    segment = collected.get("segment") or SEGMENT_FROM_SITE_TYPE.get(site_type)
    mount = "ground_mounted" if (project_type == "ground_mounted" or site_type == "ground_mounted") else "rooftop"
    if project_type == "solar_pump" or (scheme or "").startswith("pm_kusum"):
        return segment, "agricultural", mount
    if segment == "government" or site_type == "rooftop_institutional":
        return segment or "government", "government", mount
    if mount == "ground_mounted":
        return segment, "utility", mount
    if segment in COVERED_SEGMENTS:
        return segment, None, mount
    return segment, "fallback", mount


def _address(inputs, fallback: Optional[str]) -> list[str]:
    if inputs is None:
        return [fallback] if fallback else []
    lines = [line.strip().rstrip(",") for line in (inputs.full_address or "").splitlines() if line.strip()]
    whole = " ".join(lines).lower()
    tail = f"{inputs.state} – {inputs.pin_code}" if inputs.pin_code else inputs.state
    if inputs.district and inputs.district.lower() not in whole:
        tail = f"{inputs.district}, {tail}"
    return [*lines, tail] if tail else lines


def _mobile(*candidates) -> Optional[str]:
    for value in candidates:
        digits = re.sub(r"\D", "", value or "")
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if _MOBILE_RE.match(digits):
            return digits
    return None


def defaults_key(doc: ProposalDocument, section: str) -> str:
    if isinstance(doc, StudioDocument):
        return _studio_defaults_key(doc, section)
    lay = layout(doc)
    if section == "payment_terms":
        return lay
    if section == "customer_scope":
        return f"{lay}|pmsg={subsidy_applies(doc)}"
    up_to_5 = doc.plant.capacity_kwp is not None and doc.plant.capacity_kwp <= 5
    return f"{lay}|pmsg={subsidy_applies(doc)}|up5={up_to_5}|package={doc.plant.package}"


def _when(rule: Optional[str], doc: ProposalDocument) -> bool:
    if rule is None:
        return True
    up_to_5 = doc.plant.capacity_kwp is not None and doc.plant.capacity_kwp <= 5
    return {"pm_surya_ghar": subsidy_applies(doc), "not_pm_surya_ghar": not subsidy_applies(doc),
            "up_to_5kw": up_to_5, "above_5kw": not up_to_5}[rule]


def makes_text(package: Optional[str], component: str) -> str:
    """"Sova Solar / HR Solar / V-Sole / Equivalent" — the price list's makes for the
    package's component ("cables": the approved cable makes); "–" without a package."""
    from app.pricing import price_list
    if component == "cables":
        spec = price_list.load()["electrical_spec_up_to_5kw"]["ac_cable"]
    elif package:
        spec = price_list.equipment(package)[component]
    else:
        return "–"
    makes = list(spec.get("makes") or [])
    return " / ".join(makes + (["Equivalent"] if spec.get("or_equivalent") else [])) or "–"


def default_section(doc: ProposalDocument, section: str) -> list:
    if isinstance(doc, StudioDocument):
        return _studio_default_section(doc, section)
    cfg = document_config.load()
    lay = cfg[layout(doc)]
    if section == "payment_terms":
        if layout(doc) == "compact":
            from app.pricing import price_list
            return [PaymentTerm(percent=Decimal(str(m["percent"])), label=m["label"])
                    for m in price_list.commercial_terms()["payment_schedule"]]
        return [PaymentTerm(**t) for t in lay["payment_terms"]]
    if section == "customer_scope":
        return [item["text"] for item in lay["customer_scope"] if _when(item.get("when"), doc)]
    rows = []
    for row in lay["boq"]:
        if not _when(row.get("when"), doc):
            continue
        make = makes_text(doc.plant.package, row["makes_from"]) if "makes_from" in row else row["make"]
        rows.append(BoqRow(description=row["description"], quantity=row["quantity"], standard=row.get("standard"),
                           make=make, kind=row.get("kind")))
    return rows


def refresh_defaults(doc: ProposalDocument) -> list[str]:
    """Regenerate the defaulted sections staff have not edited when what they depend on
    changed (layout, PM Surya Ghar, ≤ 5 kW, package). Staff edits are never overwritten.
    Returns the sections regenerated."""
    regenerated = []
    for section in DEFAULTED_SECTIONS:
        key = defaults_key(doc, section)
        if section in doc.edited or doc.defaults.get(section) == key:
            continue
        setattr(doc, section, default_section(doc, section))
        doc.defaults[section] = key
        regenerated.append(section)
    return regenerated


def requote(doc: ProposalDocument) -> None:
    """A price-list price follows the plant: same package, the document's phase and
    capacity. A size the list does not price leaves no price (staff set one)."""
    if isinstance(doc, StudioDocument):
        _studio_requote(doc)
        return
    if doc.price.mode != "price_list":
        return
    from app.pricing import price_list
    phase = {"single": "SINGLE", "three": "THREE"}.get(doc.connection.phase or "")
    q = price_list.quote(doc.plant.package or "", phase or "", doc.plant.capacity_kwp) \
        if doc.plant.package and phase and doc.plant.capacity_kwp else None
    if isinstance(q, price_list.PriceQuote):
        doc.price = Price(mode="price_list", basic=q.basic, price_list_version=q.price_list_version)
    else:
        doc.price = Price(mode="none")


def build(db, lead, list_ctx: Optional[dict]) -> ProposalDocument:
    """The document for a new proposal version, from what the system holds."""
    from app.memory.models import Customer, CustomerOnboarding
    from app.proposals.price_list_content import _inputs_for_lead
    from sqlalchemy import select

    collected = dict(lead.collected_fields or {})
    customer = db.get(Customer, lead.customer_id)
    inputs = _inputs_for_lead(db, lead.id)
    onboarding = db.execute(select(CustomerOnboarding).where(CustomerOnboarding.lead_id == lead.id)) \
        .scalar_one_or_none()
    segment, coverage, mount = _segment_and_coverage(collected, inputs)
    organisation = inputs is not None and _val(inputs.customer_type) == "organisation"
    org_name = (inputs.organisation_name if inputs is not None else None) or (customer.company if customer else None)
    if not organisation and org_name and segment in ("commercial", "industrial") and inputs is None:
        organisation = True
    person = customer.name if customer else None
    site_lines = _address(inputs, collected.get("location"))
    addressee = Addressee(
        customer_type="organisation" if organisation else "individual",
        title="M/s" if organisation else None,
        name=org_name if organisation else person,
        attention=person if organisation and person and person != org_name else None,
        address_lines=site_lines,
        mobile=_mobile(customer.phone if customer else None, onboarding.customer_mobile if onboarding else None,
                       collected.get("phone")),
        salutation="",
    )
    phase = {"single_phase": "single", "three_phase": "three"}.get(_val(inputs.connection_phase)) \
        if inputs is not None else None
    category = segment.title() if segment and segment.title() in CATEGORIES else None
    connection = Connection(
        discom=inputs.discom if inputs is not None else None,
        consumer_number=inputs.consumer_number if inputs is not None else None,
        sanctioned_load_kw=Decimal(str(inputs.sanctioned_load_kw)) if inputs is not None and inputs.sanctioned_load_kw
        else None,
        category=category, phase=phase)
    site = Site(address_lines=site_lines, town=inputs.district if inputs is not None else collected.get("location"),
                state=inputs.state if inputs is not None else None)
    kw = (list_ctx or {}).get("kw") or (inputs.desired_output_kw if inputs is not None else None) \
        or collected.get("capacity_kw")
    try:
        capacity = Decimal(str(kw)) if kw not in (None, "") and Decimal(str(kw)) > 0 else None
    except Exception:
        capacity = None
    from app.pricing import price_list
    package = (list_ctx or {}).get("package") or (price_list.PACKAGE_FOR_TIER.get(_val(inputs.equipment_tier))
                                                  if inputs is not None else None)
    scheme = _val(inputs.scheme_name) if inputs is not None else None
    doc = ProposalDocument(
        segment=segment, coverage=coverage, mount_type=mount,
        housing_society=inputs is not None and _val(inputs.survey_variant) == "housing_society",
        addressee=addressee, connection=connection, site=site,
        pm_surya_ghar=PmSuryaGhar(subsidy_opted=scheme == "pm_surya_ghar"),
        plant=Plant(capacity_kwp=capacity, package=package,
                    dcr=bool(inputs.dcr_panels_required) if inputs is not None else False),
    )
    quote = (list_ctx or {}).get("quote") if (list_ctx or {}).get("status") == "list_price" else None
    if quote is not None:
        doc.price = Price(mode="price_list", basic=quote.basic, price_list_version=quote.price_list_version)
    refresh_defaults(doc)
    return doc


def carry_forward(previous: ProposalDocument, fresh: ProposalDocument) -> ProposalDocument:
    """A revision (a new version on the same lead) starts from what the system holds now
    (`fresh`), with every section staff edited on the previous version carried over, and
    a price staff set kept. A price-list price is re-quoted for the carried plant."""
    doc = fresh.model_copy(deep=True)
    for section in previous.edited:
        setattr(doc, section, copy.deepcopy(getattr(previous, section)))
        if section in previous.defaults:
            doc.defaults[section] = previous.defaults[section]
    doc.edited = list(previous.edited)
    if previous.price.mode in ("staff_basic", "staff_total"):
        doc.price = previous.price.model_copy()
    if "connection" in doc.edited:
        reclassify(doc)
    requote(doc)
    refresh_defaults(doc)
    return doc


# ── Editing ──────────────────────────────────────────────────────────────────────

def reclassify(doc: ProposalDocument) -> None:
    """A rooftop lead the system could not classify (coverage "fallback": no segment was
    ever collected) takes its segment from the consumer category staff choose. A lead
    recorded as government, utility-scale or agricultural keeps that — its text is
    Grid Nova's to supply (uncovered_segments)."""
    if doc.coverage not in (None, "fallback") or doc.mount_type != "rooftop":
        return
    category = (doc.connection.category or "").lower()
    if category in COVERED_SEGMENTS:
        doc.segment, doc.coverage = category, None
    elif category in ("government", "agricultural"):
        doc.segment, doc.coverage = category, category


_SECTION_MODELS = {"addressee": Addressee, "connection": Connection, "site": Site, "pm_surya_ghar": PmSuryaGhar,
                   "plant": Plant, "system": System}
# Sections whose change can move the price, the subsidy or the layout.
PRICE_SECTIONS = ("plant", "connection", "pm_surya_ghar")


def _flatten(value: Any, prefix: str) -> dict[str, Any]:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
        return out
    if isinstance(value, list):
        out = {f"{prefix}.length": len(value)}
        for i, v in enumerate(value):
            out.update(_flatten(v, f"{prefix}[{i}]"))
        return out
    return {prefix: value}


def diff(before: Any, after: Any, prefix: str) -> list[dict]:
    a, b = _flatten(before, prefix), _flatten(after, prefix)
    return [{"field": k, "before": a.get(k), "after": b.get(k)} for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def allowed_makes(db) -> set[str]:
    """Names a BOQ make cell may use: the equipment catalogue's brands, the price list's
    makes, the makes in the default rows, and the make-column words of the samples."""
    return {n.lower() for n in make_names(db)}


def make_names(db) -> set[str]:
    """allowed_makes in display case."""
    from sqlalchemy import select

    from app.memory.models import Equipment
    from app.pricing import price_list

    names = {"–"} | set(document_config.load()["boq_make_words"])
    data = price_list.load()
    for package in price_list.PACKAGES:
        for spec in data["equipment"][package].values():
            names |= set(spec.get("makes") or [])
    for spec in data["electrical_spec_up_to_5kw"].values():
        names |= set(spec.get("makes") or [])
    cfg = document_config.load()
    for row in cfg["compact"]["boq"] + cfg["detailed"]["boq"]:
        if "make" in row:
            names |= {part.strip() for part in row["make"].split("/")}
    names |= set(db.execute(select(Equipment.brand).where(Equipment.is_active.is_(True),
                                                          Equipment.is_placeholder.is_(False))).scalars())
    return {n for n in names if n}


def makes_by_kind(db) -> dict[str, list[str]]:
    """BE-19: the makes the BOQ make picker offers per row kind, display case — the price
    list's makes for that component (both packages), the default rows' makes of that kind,
    the active catalogue brands of that category, and "Equivalent" where the price list
    allows an equivalent. `other` is every allowed make (allowed_makes, any kind)."""
    from sqlalchemy import select

    from app.memory.models import Equipment, EquipmentCategory
    from app.pricing import price_list

    data = price_list.load()
    cfg = document_config.load()
    out: dict[str, set] = {"module": set(), "inverter": set(), "cable": set(), "conduit": set()}
    component = {"solar_panel": "module", "inverter": "inverter", "cables": "cable"}

    def add(kind, spec):
        out[kind] |= set(spec.get("makes") or [])
        if spec.get("or_equivalent"):
            out[kind].add("Equivalent")
    for package in price_list.PACKAGES:
        for name, spec in data["equipment"][package].items():
            if name in component:
                add(component[name], spec)
    for name, spec in data["electrical_spec_up_to_5kw"].items():
        add("conduit" if name in ("conduit_pipe", "flex_pipe") else "cable", spec)
    for row in cfg["compact"]["boq"] + cfg["detailed"]["boq"]:
        if row.get("kind") in out and "make" in row:
            out[row["kind"]] |= {part.strip() for part in row["make"].split("/") if part.strip()}
    category = {"module": EquipmentCategory.module, "inverter": EquipmentCategory.inverter}
    for kind, cat in category.items():
        out[kind] |= set(db.execute(select(Equipment.brand).where(
            Equipment.category == cat, Equipment.is_active.is_(True),
            Equipment.is_placeholder.is_(False))).scalars())
    every: dict[str, str] = {}
    for n in sorted(make_names(db)) + sorted(n for names in out.values() for n in names):
        every.setdefault(n.lower(), n)
    result = {k: sorted({n for n in v if n}, key=str.lower) for k, v in out.items()}
    result["other"] = sorted((v for k, v in every.items() if k != "–"), key=str.lower)
    return result


def apply_edit(db, doc: ProposalDocument, section: str, value: Any) -> tuple[ProposalDocument, list[dict], list[str]]:
    """Replace one section with `value` (validated). Returns (new document, field-level
    changes, sections regenerated from defaults). Raises DocumentError."""
    if section not in document_config.EDITABLE_SECTIONS:
        raise DocumentError("field_not_editable", f"'{section}' cannot be changed from the dashboard.", field=section,
                            status_code=403)
    new = doc.model_copy(deep=True)
    before = dump(doc)
    try:
        if section in _SECTION_MODELS:
            if not isinstance(value, dict):
                raise DocumentError("invalid_value", f"{section} must be an object.", field=section)
            setattr(new, section, _SECTION_MODELS[section].model_validate(value))
        elif section == "boq":
            rows = [BoqRow.model_validate(r) for r in (value or [])]
            if not rows:
                raise DocumentError("invalid_value", "The Bill of Quantity needs at least one row.", field="boq")
            allowed = allowed_makes(db)
            for i, row in enumerate(rows):
                unknown = [p.strip() for p in row.make.split("/") if p.strip() and p.strip().lower() not in allowed]
                if unknown:
                    raise DocumentError("make_not_in_catalogue",
                                        f"Unknown make(s) {unknown}: use the equipment catalogue's or the price "
                                        "list's makes, or 'Equivalent'.", field=f"boq[{i}].make")
            new.boq = rows
        elif section == "payment_terms":
            terms = [PaymentTerm.model_validate(t) for t in (value or [])]
            if not terms or sum(t.percent for t in terms) != 100:
                raise DocumentError("payment_terms_total", "Payment terms must total 100%.", field="payment_terms")
            new.payment_terms = terms
        elif section == "customer_scope":
            new.customer_scope = _bullets(list(value or []), "customer_scope")
            if not new.customer_scope:
                raise DocumentError("invalid_value", "The customer scope needs at least one item.",
                                    field="customer_scope")
        elif section == "notes":
            new.notes = _bullets(list(value or []), "notes")
        new = type(doc).model_validate(dump(new))
    except DocumentError:
        raise
    except ValueError as exc:
        raise DocumentError("invalid_value", _first_error(exc), field=section) from exc
    if section not in new.edited:
        new.edited.append(section)       # staff own this section now: never regenerated
    if section == "connection":
        reclassify(new)
    if section in PRICE_SECTIONS:
        requote(new)
    regenerated = refresh_defaults(new)
    new.revision = doc.revision + 1
    changes = diff(before, dump(new), "")
    changes = [c for c in changes if c["field"] != "revision" and not c["field"].startswith(("defaults.", "edited"))]
    return new, changes, regenerated


def _first_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        first = errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        return f"{where}: {first.get('msg')}" if where else str(first.get("msg"))
    return str(exc)


def set_price(doc: ProposalDocument, *, basic: Optional[Decimal] = None, rate_per_kwp: Optional[Decimal] = None,
              total: Optional[Decimal] = None) -> ProposalDocument:
    """adjust-pricing's write into the document: a basic price, a rate per kWp (× the
    capacity) or — the original route's `new_value` — a total including GST."""
    new = doc.model_copy(deep=True)
    if basic is not None:
        new.price = Price(mode="staff_basic", basic=money(basic))
    elif rate_per_kwp is not None:
        new.price = Price(mode="staff_basic", rate_per_kwp=money(rate_per_kwp))
    elif total is not None and total > 0:
        new.price = Price(mode="staff_total", total=money(total))
    else:
        new.price = Price(mode="none")       # a zero total: no price yet
    new.revision = doc.revision + 1
    return new


def with_document(content: Optional[dict], doc: ProposalDocument) -> dict:
    """A copy of proposal content carrying `doc`."""
    out = copy.deepcopy(content or {})
    out["document"] = dump(doc)
    return out


# ── Schema 2: Proposal Studio documents (BE-18) ─────────────────────────────────
#
# A Studio document is a version-1 document plus optional sections. It is a subclass, so
# a version-1 (lead) document is parsed, computed and rendered exactly as before; every
# schema-2 branch above is an `isinstance(doc, StudioDocument)` test.

STUDIO_SCHEMA_VERSION = 2
UNIT_RE = re.compile(r"^[A-Za-z. ]{1,12}$")


def _studio_cfg() -> dict:
    from app.studio import config as studio_config
    return studio_config.load()


class StudioProject(_Section):
    project_type: str
    houses: Optional[int] = Field(default=None, ge=1, le=100000)   # housing society: number of houses
    roof_area_sqft: Optional[Decimal] = Field(default=None, gt=0, le=Decimal("10000000"))  # shadow-free, as surveyed

    @field_validator("project_type")
    @classmethod
    def _type(cls, v):
        from app.studio.config import PROJECT_TYPES
        if v not in PROJECT_TYPES:
            raise ValueError(f"project_type must be one of {list(PROJECT_TYPES)}")
        return v


class SavingsInputs(_Section):
    """From the customer's own bill, typed by staff. Tariff is required for the page, and
    either the average monthly units or the average monthly bill."""
    monthly_units: Optional[Decimal] = Field(default=None, gt=0, le=Decimal("10000000"))
    monthly_bill: Optional[Decimal] = Field(default=None, gt=0, le=Decimal("100000000"))
    tariff_per_unit: Optional[Decimal] = Field(default=None, gt=0, le=Decimal("100"), decimal_places=2)
    fixed_charges: Optional[Decimal] = Field(default=None, ge=0, le=Decimal("10000000"))
    export_rate: Optional[Decimal] = Field(default=None, ge=0, le=Decimal("100"), decimal_places=2)


class ItemRow(_Section):
    """One line of an itemised (street lighting / supply) proposal: quantity × unit rate."""
    description: str = Field(min_length=1, max_length=160)
    specification: Optional[str] = Field(default=None, max_length=200)
    make: str = Field(min_length=1, max_length=160)
    quantity: Decimal = Field(gt=0, le=Decimal("1000000"), decimal_places=3)
    unit: str
    unit_rate: Decimal = Field(gt=0, le=Decimal("100000000"), decimal_places=2)
    category: str

    @field_validator("description", "specification", "make")
    @classmethod
    def _plain(cls, v):
        return _text_ok(_clean(v), "an item cell") if v is not None and _clean(v) else v

    @field_validator("unit")
    @classmethod
    def _unit(cls, v):
        units = _studio_cfg()["itemised"]["units"]
        if v not in units:
            raise ValueError(f"unit must be one of {units}")
        return v

    @field_validator("category")
    @classmethod
    def _category(cls, v):
        cats = _studio_cfg()["itemised"]["categories"]
        if v not in cats:
            raise ValueError(f"category must be one of {sorted(cats)}")
        return v


class Display(_Section):
    show_discount_line: bool = False
    show_savings: bool = False
    show_package_comparison: bool = False


class Discount(_Section):
    amount: Decimal = Field(gt=0, decimal_places=2)
    reason: str = Field(min_length=3, max_length=200)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v):
        return _text_ok(_clean(v) or "", "the discount reason")


class StudioPrice(Price):
    """Price plus the Studio's discount. `list_basic` is computed (the price master's basic
    for a list-priceable size, else None) and never typed; `reason` is required when the
    offered price is below the price master."""
    list_basic: Optional[Decimal] = None
    discount: Optional[Discount] = None
    reason: Optional[str] = Field(default=None, max_length=200)


class StudioDocument(ProposalDocument):
    schema_version: Literal[2] = STUDIO_SCHEMA_VERSION
    project: StudioProject
    price: StudioPrice = Field(default_factory=StudioPrice)
    savings: Optional[SavingsInputs] = None
    items: list[ItemRow] = Field(default_factory=list, max_length=60)
    display: Display = Field(default_factory=Display)
    notes_free: list[str] = Field(default_factory=list, max_length=10)   # printed under "Special Notes"
    cover_highlight: Optional[str] = Field(default=None, max_length=90)
    validity_days: int = Field(default=15, ge=1, le=90)

    @field_validator("notes_free")
    @classmethod
    def _notes_free(cls, v):
        return _bullets(v, "notes_free")

    @field_validator("cover_highlight")
    @classmethod
    def _highlight(cls, v):
        return _text_ok(_clean(v), "the cover highlight") if v is not None and _clean(v) else None


def parse_any(raw: dict) -> ProposalDocument:
    """A stored document of either schema."""
    if isinstance(raw, dict) and raw.get("schema_version") == STUDIO_SCHEMA_VERSION:
        return StudioDocument.model_validate(raw)
    return ProposalDocument.model_validate(raw)


def studio_project(doc: StudioDocument) -> dict:
    from app.studio import config as studio_config
    return studio_config.project_type(doc.project.project_type)


def new_studio_document(project_type: str, *, validity_days: Optional[int] = None) -> StudioDocument:
    """A fresh Studio document for a project type: segment, coverage, mount and category
    from config/studio.yaml, the defaulted sections generated."""
    from app.studio import config as studio_config
    spec = studio_config.project_type(project_type)
    doc = StudioDocument(project=StudioProject(project_type=project_type),
                         validity_days=validity_days or studio_config.validity_days())
    apply_project_type(doc)
    refresh_defaults(doc)
    return doc


def apply_project_type(doc: StudioDocument) -> None:
    """What the project type decides: segment, coverage, mount and housing society."""
    spec = studio_project(doc)
    doc.segment, doc.coverage = spec["segment"], spec["coverage"]
    doc.mount_type = spec["mount"] or "rooftop"
    doc.housing_society = bool(spec.get("housing_society"))
    if spec["category"] and doc.connection.category is None:
        doc.connection.category = spec["category"]


def _studio_defaults_key(doc: StudioDocument, section: str) -> str:
    lay = layout(doc)
    cap = doc.plant.capacity_kwp
    up_to_10 = cap is not None and cap <= document_config.compact_max_kwp()
    if section == "payment_terms":
        return f"v2|{lay}|le10={up_to_10}"
    up_to_5 = cap is not None and cap <= 5
    return f"v2|{lay}|pmsg={subsidy_applies(doc)}|up5={up_to_5}|package={doc.plant.package}"


def _studio_default_section(doc: StudioDocument, section: str) -> list:
    """Payment terms by size (≤ 10 kW: the price list's 20/60/20; above: 25/60/15), the
    layout's BOQ and customer scope. The itemised layout has no default BOQ or scope."""
    lay = layout(doc)
    if section == "payment_terms":
        if lay == "itemised":
            return [PaymentTerm(**t) for t in _studio_cfg()["payment_terms"]["itemised"]]
        cap = doc.plant.capacity_kwp
        if cap is not None and cap <= document_config.compact_max_kwp():
            from app.pricing import price_list
            return [PaymentTerm(percent=Decimal(str(m["percent"])), label=m["label"])
                    for m in price_list.commercial_terms()["payment_schedule"]]
        return [PaymentTerm(**t) for t in document_config.load()["detailed"]["payment_terms"]]
    if lay == "itemised":
        return []
    cfg = document_config.load()[lay]
    if section == "customer_scope":
        return [item["text"] for item in cfg["customer_scope"] if _when(item.get("when"), doc)]
    rows = []
    for row in cfg["boq"]:
        if not _when(row.get("when"), doc):
            continue
        make = makes_text(doc.plant.package, row["makes_from"]) if "makes_from" in row else row["make"]
        rows.append(BoqRow(description=row["description"], quantity=row["quantity"], standard=row.get("standard"),
                           make=make, kind=row.get("kind")))
    return rows


def master_quote(doc: ProposalDocument):
    """The price master's quote for the document's package, phase and capacity, or None
    when the size is not list-priceable."""
    from app.pricing import price_list
    phase = {"single": "SINGLE", "three": "THREE"}.get(doc.connection.phase or "")
    if not (doc.plant.package and phase and doc.plant.capacity_kwp):
        return None
    q = price_list.quote(doc.plant.package, phase, doc.plant.capacity_kwp)
    return q if isinstance(q, price_list.PriceQuote) else None


def _studio_requote(doc: StudioDocument) -> None:
    """list_basic follows the plant; a price-master price follows it too. The discount and
    the below-master reason stay as staff set them."""
    q = master_quote(doc) if layout(doc) != "itemised" else None
    price = doc.price
    price.list_basic = q.basic if q is not None else None
    if price.mode == "price_list":
        if q is not None:
            price.basic, price.price_list_version = q.basic, q.price_list_version
        else:
            price.mode, price.basic, price.price_list_version = "none", None, None


def studio_subsidy(doc: StudioDocument, total: Optional[Decimal]) -> Optional[dict]:
    """PM Surya Ghar GHS/RWA CFA for a housing society: ₹18,000/kW (special category
    ₹19,800) on min(installed, 3 kW × houses, 500 kW) — app/estimate/subsidy.py. Only when
    opted, with DCR modules and the number of houses known."""
    if not (doc.housing_society and doc.pm_surya_ghar.subsidy_opted and doc.plant.dcr
            and doc.plant.capacity_kwp and doc.project.houses):
        return None
    from app.estimate import subsidy as pm_surya_ghar
    soc = pm_surya_ghar._society(Decimal(str(doc.plant.capacity_kwp)), doc.project.houses,
                                 pm_surya_ghar.category(doc.site.state))
    amount = money(soc["total"])
    return {"applies": True, "kind": "housing_society",
            "slabs": [{"kw": kw_text(soc["eligible_kwp"]), "rate": str(money(soc["rate_per_kwp"])),
                       "amount": str(amount)}],
            "state_lines": [], "total": str(amount),
            "effective_cost": str(total - amount) if total is not None else None,
            "houses": doc.project.houses, "eligible_kw": kw_text(soc["eligible_kwp"]),
            "limited_by": soc["limited_by"], "kw_per_house": kw_text(soc["kwp_per_house"]),
            "max_kw": kw_text(soc["max_kwp"])}


def package_comparison(doc: ProposalDocument) -> Optional[list[dict]]:
    """Nova Assured vs Nova Signature for the same capacity and phase, both from the price
    master (Signature = Assured × 1.12 before GST): None when the size is not list-priced."""
    from app.pricing import price_list
    phase = {"single": "SINGLE", "three": "THREE"}.get(doc.connection.phase or "")
    if not (phase and doc.plant.capacity_kwp):
        return None
    out = []
    for package in price_list.PACKAGES:
        q = price_list.quote(package, phase, doc.plant.capacity_kwp)
        if not isinstance(q, price_list.PriceQuote):
            return None
        out.append({"package": package, "name": price_list.display_name(package),
                    "panels": makes_text(package, "solar_panel"), "inverters": makes_text(package, "inverter"),
                    "basic": str(q.basic), "gst": str(q.gst), "total": str(q.final),
                    "chosen": package == doc.plant.package})
    return out


def _itemised_figures(doc: StudioDocument) -> dict:
    """Quantity × unit rate per line, GST per line at its category's rate, grouped by rate."""
    from app.pricing.amount_words import amount_in_words
    from app.studio import config as studio_config
    lines, groups = [], {}
    for i, item in enumerate(doc.items, 1):
        rate = Decimal(str(studio_config.gst_category(item.category)["gst_rate"]))
        amount = money(item.quantity * item.unit_rate)
        gst = money(amount * rate)
        lines.append({"no": i, "amount": str(amount), "gst_rate": str(rate), "gst": str(gst),
                      "gst_percent": kw_text(rate * 100)})
        group = groups.setdefault(rate, {"rate": str(rate), "percent": kw_text(rate * 100), "taxable": Decimal(0),
                                         "gst": Decimal(0)})
        group["taxable"] += amount
        group["gst"] += gst
    subtotal = sum((Decimal(line["amount"]) for line in lines), Decimal(0))
    gst_total = sum((g["gst"] for g in groups.values()), Decimal(0))
    total = subtotal + gst_total if lines else None
    payments = []
    if total is not None and doc.payment_terms:
        running = Decimal(0)
        for i, term in enumerate(doc.payment_terms):
            amount = total - running if i == len(doc.payment_terms) - 1 else money(total * term.percent / 100)
            running += amount
            payments.append({"percent": kw_text(term.percent), "label": term.label, "short_label": term.short_label,
                             "amount": str(amount)})
    amounts = [Decimal(x["amount"]) for x in lines] + [Decimal(p["amount"]) for p in payments]
    amounts += [g["gst"] for g in groups.values()] + ([total] if total is not None else [])
    return {
        "layout": "itemised", "schema_version": STUDIO_SCHEMA_VERSION, "project_type": doc.project.project_type,
        "capacity_kwp": None, "price_mode": "items",
        "basic": str(subtotal) if lines else None, "gst": str(gst_total) if lines else None,
        "total": str(total) if total is not None else None, "gst_rate": None,
        "words": {"basic": amount_in_words(subtotal) if lines else None,
                  "gst": amount_in_words(gst_total) if lines else None,
                  "total": amount_in_words(total) if total is not None else None},
        "payments": payments, "whole_rupees": all(a == a.to_integral_value() for a in amounts),
        "items": lines,
        "gst_groups": [{"rate": g["rate"], "percent": g["percent"], "taxable": str(g["taxable"]),
                        "gst": str(g["gst"])} for _, g in sorted(groups.items())],
        "subsidy": {"applies": False, "slabs": [], "state_lines": [], "total": None, "effective_cost": None},
        "generation": {"units_per_kwp_per_month": None, "monthly_units": None, "annual_units": None},
        "space_required_sqft": None, "validity_days": doc.validity_days,
        "list_basic": None, "discount": None, "below_price_master": None, "savings": None,
        "package_comparison": None,
    }


def studio_figures(doc: StudioDocument) -> dict:
    """The plant figures (the same calculation as a lead proposal) plus the Studio's:
    discount, housing-society CFA, validity, savings and the package comparison."""
    if layout(doc) == "itemised":
        return _itemised_figures(doc)
    out = _plant_figures(doc)
    out["schema_version"] = STUDIO_SCHEMA_VERSION
    out["project_type"] = doc.project.project_type
    out["validity_days"] = doc.validity_days
    cap = doc.plant.capacity_kwp
    before = basic_before_discount(doc.price, cap) if doc.price.mode != "staff_total" else None
    discount = doc.price.discount
    out["discount"] = ({"price_before_discount": str(before), "amount": str(money(discount.amount)),
                        "reason": discount.reason} if discount is not None and before is not None else None)
    out["list_basic"] = str(doc.price.list_basic) if doc.price.list_basic is not None else None
    below = None
    if doc.price.list_basic is not None and out["basic"] is not None:
        list_basic, basic = Decimal(str(doc.price.list_basic)), Decimal(out["basic"])
        if basic < list_basic:
            below = {"list_basic": str(list_basic), "basic": str(basic),
                     "percent": str(((list_basic - basic) / list_basic * 100).quantize(Decimal("0.01"),
                                                                                       rounding=ROUND_HALF_UP))}
    out["below_price_master"] = below
    total = Decimal(out["total"]) if out["total"] is not None else None
    society = studio_subsidy(doc, total)
    if society is not None:
        out["subsidy"] = society
    effective = Decimal(out["subsidy"]["effective_cost"]) if out["subsidy"].get("effective_cost") else total
    from app.studio import savings as studio_savings
    out["savings"] = studio_savings.compute(doc, out, effective)
    out["package_comparison"] = package_comparison(doc) if (cap is not None and cap <= document_config.compact_max_kwp()
                                                            and layout(doc) == "compact") else None
    amounts = [Decimal(a) for a in (out["basic"], out["gst"], out["total"], out["subsidy"].get("total"),
                                    out["subsidy"].get("effective_cost")) if a]
    amounts += [Decimal(p["amount"]) for p in out["payments"]]
    if out["discount"]:
        amounts += [Decimal(out["discount"]["price_before_discount"]), Decimal(out["discount"]["amount"])]
    out["whole_rupees"] = all(a == a.to_integral_value() for a in amounts)
    return out


def studio_missing_required(doc: StudioDocument, figs: Optional[dict] = None) -> list[dict]:
    """What still stops a Studio proposal being approved or issued (besides the proof-check).
    The uncovered-segment text is reported separately (content_blockers → content_not_ready)."""
    figs = figs or figures(doc)
    if layout(doc) == "itemised":
        out = []
        a = doc.addressee
        if not a.name:
            out.append({"field": "addressee.name", "message": "the customer's name"})
        if not a.address_lines:
            out.append({"field": "addressee.address_lines", "message": "the customer's address"})
        if not doc.items:
            out.append({"field": "items", "message": "at least one line item"})
        if not doc.payment_terms:
            out.append({"field": "payment_terms", "message": "the payment terms"})
        if not doc.customer_scope:
            out.append({"field": "customer_scope", "message": "at least one customer-scope item"})
        if not doc.site.state:
            out.append({"field": "site.state", "message": "the site's state"})
        return out
    out = [({"field": "price", "message": "a price"} if m["field"] == "price" else m)
           for m in missing_required(doc, figs)
           if m["field"] != "segment_text" and not (m["field"] == "plant.mount_type"
                                                    and studio_project(doc)["mount"] == "ground_mounted")]
    if doc.housing_society and not doc.project.houses:
        out.append({"field": "project.houses", "message": "the number of houses in the society"})
    if doc.display.show_savings:
        s = doc.savings
        if s is None or s.tariff_per_unit is None or (s.monthly_units is None and s.monthly_bill is None):
            out.append({"field": "savings", "message": "the customer's tariff and average monthly units or bill "
                                                       "(for the savings page)"})
    if doc.display.show_discount_line and doc.price.discount is None:
        out.append({"field": "price.discount", "message": "a discount (or switch the discount line off)"})
    if figs.get("below_price_master") and not doc.price.reason and doc.price.discount is None:
        out.append({"field": "price.reason", "message": "the reason the price is below the price master"})
    return out


def content_blockers(doc: StudioDocument) -> list[dict]:
    """[{registry, message}] — Grid Nova text or rates still missing for this proposal type:
    the uncovered segment's scope text (PRO-7, PRO-8, PRO-10, PRO-11, PRO-12), an itemised
    category's unconfirmed GST rate or missing warranty rows. Draft and preview work; approve
    and issue answer 409 content_not_ready."""
    from app.placeholders import registry
    out = []
    if doc.coverage is not None:
        keys = document_config.load()["uncovered_segments"].get(doc.coverage, [])
        reg = registry.load()
        for key in keys:
            if not _open_section(key):
                continue
            ids = [e.id for e in reg.entries if e.file == "config/proposal_content.yaml"
                   and (e.location or {}).get("key") == key]
            for eid in ids or [key]:
                out.append({"registry": eid, "message": f"Grid Nova's text for {doc.coverage} proposals ({key})"})
    if layout(doc) == "itemised":
        seen = set()
        for item in doc.items:
            spec = _studio_cfg()["itemised"]["categories"][item.category]
            for eid, what in ((spec.get("registry"), f"the GST rate for {spec['label']}"),
                              (spec.get("warranty_registry"), f"the warranty terms for {spec['label']}")):
                if eid and eid not in seen and registry.unresolved_ids([eid]):
                    seen.add(eid)
                    out.append({"registry": eid, "message": f"Grid Nova's confirmation of {what}"})
    return out
