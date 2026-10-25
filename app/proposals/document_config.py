"""config/proposal_document.yaml — the two fixed proposal layouts (BE-13): brand, signatory,
layout rule, every fixed sentence, default rows and who may edit which section.

Loaded once and validated at startup (app/main.py lifespan): a malformed file, an unknown
{token} in a fixed sentence, a missing brand image, an unknown section or role in
edit_permissions, or payment terms that do not total 100 stop the boot rather than
produce a broken customer document.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parents[2] / "config" / "proposal_document.yaml"
ASSETS = Path(__file__).resolve().parents[1] / "pdf" / "assets"

LAYOUTS = ("compact", "detailed")
# The parts of the document the dashboard may change; everything else is fixed here.
EDITABLE_SECTIONS = ("addressee", "connection", "site", "pm_surya_ghar", "plant", "system", "boq",
                     "customer_scope", "payment_terms", "notes")
# Staff roles that can see leads at all (app/auth/scoping.py); operations and accounts never can.
EDITOR_ROLES = ("admin", "sales", "management", "engineer")
# {tokens} a fixed sentence may use — filled from the document and its figures.
TOKENS = frozenset({
    "capacity_kwp", "customer_short_name", "site_town", "site_state", "discom", "sub_division", "phase_hyphen",
    "phase_words", "phase_title", "phase_cap", "phase_supply", "package_name", "module_technology",
    "module_phrase", "module_short", "module_pair", "dcr_label", "approved_capacity_kwp", "subsidy_up_to",
    "standard_warranty_years", "validity_days", "space_required_sqft", "space_low", "space_high", "list_kw",
    "location", "package_clause", "package_suffix", "total", "subsidy", "effective_cost", "per_kwp", "monthly",
    "annual", "dcr_plant_label", "category", "inverter", "mounting", "grid_integration", "space",
    "closing_working_with", "standard_warranty", "o_and_m", "panel_life_span", "kw", "rate",
})
# Tokens staff may keep in text they edit (BOQ rows, customer-scope bullets, notes):
# values that follow the plant when it changes.
EDITABLE_TOKENS = frozenset({"capacity_kwp", "module_technology", "dcr_label", "phase_cap", "discom",
                             "space_required_sqft", "validity_days"})
_TOKEN_RE = re.compile(r"\{([a-z_0-9]+)\}")
_BOQ_KEYS = {"description", "quantity", "standard", "make", "makes_from", "kind", "when"}
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_WARRANTY_RE = re.compile(r"^(\d+) years from handover$")
_REQUIRED_TOP = ("schema_version", "colours", "brand", "signatory", "layout_rule",
                 "salutations", "default_salutation", "titles", "edit_permissions", "boq_make_words",
                 "module_phrases", "common", "compact", "detailed", "uncovered_segments")


class DocumentConfigError(RuntimeError):
    pass


def tokens_in(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text or ""))


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _validate(data: dict) -> dict:
    def need(cond, message: str) -> None:
        if not cond:
            raise DocumentConfigError(f"{_PATH.name}: {message}")

    need(isinstance(data, dict), "must be a mapping")
    missing = [k for k in _REQUIRED_TOP if k not in data]
    need(not missing, f"missing keys: {missing}")
    need(data["schema_version"] == 1, "schema_version must be 1")
    for name, value in data["colours"].items():
        need(isinstance(value, str) and _HEX_RE.match(value), f"colours.{name} must be a #RRGGBB colour")
    for key in ("header_image", "watermark_image", "letterhead_image"):
        need((ASSETS / str(data["brand"].get(key))).is_file(), f"brand.{key}: no such file in app/pdf/assets")
    need(str(data["brand"].get("qr_url", "")).startswith("https://"), "brand.qr_url must be an https URL")
    signatory = data["signatory"]
    need(all(isinstance(signatory.get(k), str) and signatory[k].strip() for k in ("name", "title", "for_line")),
         "signatory needs name, title and for_line")
    try:
        compact_max = Decimal(str(data["layout_rule"]["compact_max_kwp"]))
    except (InvalidOperation, KeyError, TypeError):
        compact_max = Decimal(0)
    need(compact_max > 0, "layout_rule.compact_max_kwp must be a positive number")
    # BE-14: generation per kWp and roof area per kWp are the solar estimate engine's
    # (config/solar_estimate.yaml) — one source for proposals and the calculator.
    for key in ("generation", "space_required"):
        need(key not in data, f"{key} is now in config/solar_estimate.yaml (generation / roof_area); remove it here")
    need(data["default_salutation"] in data["salutations"], "default_salutation must be one of salutations")
    for section, roles in data["edit_permissions"].items():
        need(section in EDITABLE_SECTIONS, f"edit_permissions: unknown section {section!r}")
        need(isinstance(roles, list) and set(roles) <= set(EDITOR_ROLES),
             f"edit_permissions.{section}: roles must be among {EDITOR_ROLES}")
    terms = data["detailed"]["payment_terms"]
    need(sum(Decimal(str(t["percent"])) for t in terms) == 100, "detailed.payment_terms must total 100")
    for text in _strings({k: data[k] for k in ("common", "compact", "detailed", "module_phrases")}):
        unknown = tokens_in(text) - TOKENS
        need(not unknown, f"unknown placeholder(s) {sorted(unknown)} in: {text[:60]}")
    for rows in (data["compact"]["boq"], data["detailed"]["boq"]):
        for row in rows:
            need(row.get("description") and row.get("quantity") and ("make" in row or "makes_from" in row),
                 f"every BOQ row needs description, quantity and make / makes_from: {row}")
            # A comma in an unquoted flow mapping silently makes a new key — refuse it.
            need(set(row) <= _BOQ_KEYS, f"BOQ row has unexpected keys {sorted(set(row) - _BOQ_KEYS)} "
                                        f"(quote values that contain commas): {row}")
            need(row.get("makes_from") in (None, "solar_panel", "inverter", "cables"),
                 f"makes_from must be solar_panel, inverter or cables: {row}")
            need(row.get("when") in (None, "pm_surya_ghar", "not_pm_surya_ghar", "up_to_5kw", "above_5kw"),
                 f"unknown `when` in BOQ row: {row}")
    from app.pricing import price_list
    need(_WARRANTY_RE.match(price_list.commercial_terms()["standard_warranty"]) is not None,
         "price_list.yaml commercial_terms.standard_warranty must read 'N years from handover' "
         "(the Compact letter prints 'N-year standard warranty')")
    from app.proposals import content_engine
    library = content_engine.load_content_config().get("sections_library") or {}
    for segment, keys in data["uncovered_segments"].items():
        for key in keys:
            need(key.startswith("sections_library.") and key.split(".", 1)[1] in library,
                 f"uncovered_segments.{segment}: {key} is not in proposal_content.yaml")
    return data


@lru_cache(maxsize=1)
def load() -> dict:
    with _PATH.open(encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


def compact_max_kwp() -> Decimal:
    return Decimal(str(load()["layout_rule"]["compact_max_kwp"]))


def layout_for(capacity_kwp) -> str:
    """capacity ≤ compact_max_kwp → compact; above → detailed. Unknown capacity → compact
    (the document is incomplete then and cannot be approved anyway)."""
    if capacity_kwp in (None, ""):
        return "compact"
    return "compact" if Decimal(str(capacity_kwp)) <= compact_max_kwp() else "detailed"


def edit_roles(section: str) -> tuple[str, ...]:
    return tuple(load()["edit_permissions"].get(section) or ())


def standard_warranty_years() -> int:
    from app.pricing import price_list
    return int(_WARRANTY_RE.match(price_list.commercial_terms()["standard_warranty"]).group(1))


def asset_path(key: str) -> Path:
    return ASSETS / load()["brand"][key]
