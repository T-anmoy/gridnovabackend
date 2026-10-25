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
    """The proposal's document, or None for a proposal drafted before BE-13."""
    raw = (content or {}).get("document")
    return parse(raw) if isinstance(raw, dict) else None


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
    return document_config.layout_for(doc.plant.capacity_kwp)


def subsidy_applies(doc: ProposalDocument) -> bool:
    """PM Surya Ghar CFA: residential (not a housing society), subsidy opted, DCR modules."""
    return (doc.segment == "residential" and not doc.housing_society and doc.pm_surya_ghar.subsidy_opted
            and doc.plant.dcr and doc.plant.capacity_kwp is not None)


def price_parts(price: Price, capacity) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """(basic, gst, total). GST is 8.9% of the basic price (5% on 70% + 18% on 30%) and
    the total is basic + GST — except a total staff set as a whole (adjust-pricing's
    `new_value`), which stays exactly as set: basic = total / 1.089, GST = the rest."""
    from app.pricing.price_list import gst_of
    if price.mode == "staff_total" and price.total:
        total = money(price.total)
        basic = money(total / (1 + GST_RATE))
        return basic, total - basic, total
    basic = price.basic
    if price.mode == "staff_basic" and price.rate_per_kwp and capacity:
        basic = money(Decimal(str(price.rate_per_kwp)) * Decimal(str(capacity)))
    if not basic:
        return None, None, None
    basic = money(basic)
    gst = gst_of(basic)
    return basic, gst, basic + gst


def figures(doc: ProposalDocument) -> dict:
    """Every computed figure on the document, as decimal strings where money. One
    function: the PDF, the dashboard and the accept page all read this."""
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
                        "message": f"Grid Nova's text for {doc.coverage} proposals (still a placeholder in "
                                   f"config/proposal_content.yaml: {', '.join(keys)})"})
    return out


def _open_section(key: str) -> bool:
    from app.proposals import content_engine
    value = (content_engine.load_content_config().get("sections_library") or {}).get(key.split(".", 1)[1])
    return value is None or bool(_MARKERS.search(str(value)))


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
    return {n.lower() for n in names if n}


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
        new = ProposalDocument.model_validate(dump(new))
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
