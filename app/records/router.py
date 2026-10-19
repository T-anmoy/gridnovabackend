"""The deterministic planner: Nova Records' plan when there is no model (mock mode) or
the model's plan cannot be used (BE-9 §3).

It reads the question for things it can recognise without understanding language —
reference ids, place names from the territory master and the archive, makes from the
price list and the equipment master, sizes, years, "how many" / "total" / "outstanding"
/ "warranty" / "near" — and turns them into at most three tool calls. When it recognises
nothing it asks for something it can search on rather than guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from app.memory.models import ArchivedProject, Equipment, EquipmentCategory, TerritoryBlock, TerritoryDistrict, \
    TerritoryState
from app.records import access

_RE_LIVE_REF = re.compile(r"\bGN[ER]-[A-Z]{2}-[A-Z]{2,4}-\d{4}-\d{3,6}\b", re.I)
_RE_ARC_REF = re.compile(r"(?<![\w-])ARC-(\d+)\b", re.I)
_RE_ASSOCIATE = re.compile(r"\bGNA-[A-Z]{2}-\d{2}-\d{3,6}\b", re.I)
_RE_LEAD = re.compile(r"\b(?:lead\s*(?:#|no\.?|number|id)?\s*|L)(\d{1,9})\b", re.I)
_RE_TOKEN_REF = re.compile(r"\b[A-Z][A-Z0-9]{1,9}(?:[-/][A-Z0-9]{1,12}){1,5}\b")
_RE_KW_MIN = re.compile(r"(\d+(?:\.\d+)?)\s*kw\s*(?:\+|plus\b|and\s+above|or\s+(?:more|above|bigger|larger)|"
                        r"(?:and\s+)?over\b)|(?:above|over|more\s+than|at\s+least|minimum|min\.?)\s*(\d+(?:\.\d+)?)\s*kw",
                        re.I)
_RE_KW_MAX = re.compile(r"(?:under|below|less\s+than|up\s+to|upto|at\s+most|max(?:imum)?\.?)\s*(\d+(?:\.\d+)?)\s*kw|"
                        r"(\d+(?:\.\d+)?)\s*kw\s*(?:or\s+(?:less|below|smaller)|and\s+below)", re.I)
_RE_KW_EXACT = re.compile(r"\b(\d+(?:\.\d+)?)\s*kw(?:p)?\b", re.I)
_RE_YEAR = re.compile(r"\b(?:in|during|for|of|year)\s+(20\d{2})\b|\b(20\d{2})\b", re.I)
_RE_COORDS = re.compile(r"(-?\d{1,2}\.\d{2,})\s*[, ]\s*(-?\d{2,3}\.\d{2,})")
_RE_NEAR = re.compile(r"\b(?:near|nearby|close\s+to|around|next\s+to|within\s+(\d+(?:\.\d+)?)\s*km\s+of)\s+"
                      r"(?:the\s+)?([A-Za-z][A-Za-z .'-]{1,60}?)(?=\s+(?:with|that|which|having|using|and|in|for|"
                      r"commissioned|installed)\b|[?,.!]|$)", re.I)
_RE_KM = re.compile(r"\b(\d+(?:\.\d+)?)\s*km\b", re.I)
_RE_DAYS = re.compile(r"\b(?:next|coming|within)\s+(\d{1,4})\s*(day|days|week|weeks|month|months|year|years)\b", re.I)
_RE_NAME_TITLE = re.compile(r"\b(?i:mr|mrs|ms|miss|shri|smt|dr|sri)\.?\s+([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+)?)")
_RE_CUSTOMER_NAMED = re.compile(r"\b(?i:customer)\s+(?i:named\s+|called\s+)?([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+)?)")

SCHEME_WORDS = (
    (r"surya\s*ghar|pmsg|residential", "pm_surya_ghar_residential"),
    (r"housing\s+societ", "housing_society"),
    (r"kusum|solar\s+pumps?|\bpumps?\b", "pm_kusum_pump"),
    (r"street\s*(?:light|lamp)", "street_lighting"),
    (r"wbreda|schools?", "wbreda_school"),
    (r"ground[\s-]*mounted", "ground_mounted"),
    (r"institutional|government|govt", "institutional_govt"),
    (r"industrial|factory", "industrial"),
    (r"commercial", "commercial"),
)
STAGE_WORDS = (
    (r"passed\s+inspection|net[\s-]*meter(?:ed|ing)?|after\s+inspection|inspected", "inspection_net_meter"),
    (r"handed\s+over|handover\s+complete", "handover_complete"),
    (r"subsidy\s+received", "subsidy_received"),
    (r"subsidy\s+claimed", "subsidy_claimed"),
    (r"material\s+dispatched", "material_dispatched"),
    (r"agreement\s+signed|signed\s+agreement", "agreement_signed"),
)
GROUP_WORDS = (
    (r"(?:by|per|each|every|across)\s+block|block[\s-]*wise|which\s+block", "block"),
    (r"(?:by|per|each|every|across)\s+district|district[\s-]*wise|which\s+district", "district"),
    (r"(?:by|per|each|every|across)\s+state|state[\s-]*wise|which\s+state", "state"),
    (r"(?:by|per|each)\s+(?:month|monthly)|month[\s-]*wise|month\s+by\s+month", "month"),
    (r"(?:by|per|each)\s+year|year[\s-]*wise|year\s+by\s+year|yearly", "year"),
    (r"(?:by|per|each)\s+scheme|scheme[\s-]*wise|which\s+scheme", "scheme"),
    (r"(?:by|per|each)\s+stage|stage[\s-]*wise|which\s+stage", "stage"),
    (r"which\s+associate|(?:by|per|each)\s+associate|associate[\s-]*wise|top\s+associates?", "associate"),
    (r"(?:by|per|each)\s+(?:inverter\s+)?(?:make|brand)\s+of\s+inverter|inverter\s+(?:make|brand)s?\s+(?:used|split)",
     "inverter_make"),
    (r"(?:by|per|each)\s+(?:module\s+|panel\s+)?(?:make|brand)|which\s+(?:module|panel)\s+(?:make|brand)", "module_make"),
)


@dataclass
class Vocabulary:
    states: list = field(default_factory=list)
    districts: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    module_makes: list = field(default_factory=list)
    inverter_makes: list = field(default_factory=list)
    archive_refs: set = field(default_factory=set)


def vocabulary(db: Session) -> Vocabulary:
    from app.pricing import price_list

    data = price_list.load()
    v = Vocabulary()
    v.states = sorted({n for (n,) in db.execute(select(TerritoryState.name))}
                      | {n for (n,) in db.execute(select(distinct(ArchivedProject.state))) if n}, key=len, reverse=True)
    v.districts = sorted({n for (n,) in db.execute(select(TerritoryDistrict.name))}
                         | {n for (n,) in db.execute(select(distinct(ArchivedProject.district))) if n},
                         key=len, reverse=True)
    packages = [data["equipment"][p] for p in price_list.PACKAGES]
    v.blocks = sorted({n for (n,) in db.execute(select(TerritoryBlock.name))}
                      | {n for (n,) in db.execute(select(distinct(ArchivedProject.block))) if n}, key=len, reverse=True)
    modules = {m for pkg in packages for m in (pkg.get("solar_panel", {}).get("makes") or [])}
    inverters = {m for pkg in packages for m in (pkg.get("inverter", {}).get("makes") or [])}
    modules |= {n for (n,) in db.execute(select(distinct(ArchivedProject.module_make))) if n}
    inverters |= {n for (n,) in db.execute(select(distinct(ArchivedProject.inverter_make))) if n}
    for brand, category in db.execute(select(Equipment.brand, Equipment.category).where(
            Equipment.is_placeholder.is_(False))):
        (inverters if category == EquipmentCategory.inverter else modules).add(brand)
    v.module_makes = sorted(modules, key=len, reverse=True)
    v.inverter_makes = sorted(inverters, key=len, reverse=True)
    v.archive_refs = {r for (r,) in db.execute(select(ArchivedProject.grid_nova_ref).where(
        ArchivedProject.grid_nova_ref.isnot(None), ArchivedProject.voided_at.is_(None)))}
    return v


def _word(name: str, text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.I) is not None


def _first(names: list, text: str) -> Optional[str]:
    return next((n for n in names if _word(n, text)), None)


def quarter_bounds(today: date) -> tuple[date, date]:
    start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    end = date(start.year + (start.month + 3 > 12), (start.month + 2) % 12 + 1, 1) - timedelta(days=1)
    return start, end


def _period(q: str, today: date) -> Optional[tuple[date, date]]:
    if re.search(r"\bthis\s+quarter\b", q, re.I):
        return quarter_bounds(today)
    if re.search(r"\blast\s+quarter\b", q, re.I):
        start, _ = quarter_bounds(today)
        return quarter_bounds(start - timedelta(days=1))
    if re.search(r"\bthis\s+month\b", q, re.I):
        return date(today.year, today.month, 1), today
    if re.search(r"\blast\s+month\b", q, re.I):
        end = date(today.year, today.month, 1) - timedelta(days=1)
        return date(end.year, end.month, 1), end
    if re.search(r"\bthis\s+year\b", q, re.I):
        return date(today.year, 1, 1), today
    if re.search(r"\blast\s+year\b", q, re.I):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if m := _RE_YEAR.search(q):
        year = int(m.group(1) or m.group(2))
        return date(year, 1, 1), date(year, 12, 31)
    return None


def _days(q: str) -> int:
    m = _RE_DAYS.search(q)
    if not m:
        return 90
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit.rstrip("s")]


def _filters(db: Session, q: str, viewer, vocab: Vocabulary, today: date, *, created: bool) -> dict:
    f: dict = {}
    block = _first(vocab.blocks, q)
    district = _first([d for d in vocab.districts if not (block and d.lower() in block.lower())], q)
    if block and access.sees(viewer, "block") and (not district or block.lower() != district.lower()):
        f["block"] = block
    state = _first(vocab.states, q)
    if district:
        f["district"] = district
    if state and (not district or state.lower() != district.lower()):
        f["state"] = state
    inverter_context = re.search(r"\binverters?\b", q, re.I)
    for make in vocab.inverter_makes:
        if _word(make, q) and (inverter_context or make not in vocab.module_makes):
            f["inverter_make"] = make
            break
    for make in vocab.module_makes:
        if _word(make, q) and make != f.get("inverter_make"):
            f["module_make"] = make
            break
    if m := _RE_KW_MIN.search(q):
        f["capacity_min_kw"] = m.group(1) or m.group(2)
    if m := _RE_KW_MAX.search(q):
        f["capacity_max_kw"] = m.group(1) or m.group(2)
    if not ({"capacity_min_kw", "capacity_max_kw"} & set(f)) and (m := _RE_KW_EXACT.search(q)) \
            and not re.search(r"\b(?:total|sum|average|avg)\b", q, re.I):
        f["capacity_min_kw"] = f["capacity_max_kw"] = m.group(1)
    for pattern, scheme in SCHEME_WORDS:
        if re.search(pattern, q, re.I):
            f["scheme"] = scheme
            break
    for pattern, stage in STAGE_WORDS:
        if re.search(pattern, q, re.I):
            f["stage"] = stage
            break
    period = _period(q, today)
    if period:
        key = "created" if created else "commissioned"
        f[f"{key}_from"], f[f"{key}_to"] = period[0].isoformat(), period[1].isoformat()
    name = _RE_NAME_TITLE.search(q) or _RE_CUSTOMER_NAMED.search(q)
    if name and access.sees(viewer, "customer_name"):
        f["customer_name"] = name.group(1)
        # "Mr Sen in Salt Lake": a place that is not a district or state is searched in the
        # address (where the role may see addresses).
        place = re.search(r"\b(?:in|at|from)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)", q[name.end():])
        if place and not _first(vocab.districts + vocab.states + vocab.blocks, place.group(1)) and access.sees(viewer, "address_text"):
            f["text"] = place.group(1)
    if m := _RE_COORDS.search(q):
        km = _RE_KM.search(q)
        f["near"] = {"lat": float(m.group(1)), "lng": float(m.group(2)), **({"km": float(km.group(1))} if km else {})}
    elif m := _RE_NEAR.search(q):
        place = m.group(2).strip(" .'-")
        km = m.group(1) or (_RE_KM.search(q).group(1) if _RE_KM.search(q) else None)
        if place and not re.fullmatch(r"(?:me|here|us|it|this|that)", place, re.I):
            f["near"] = {"place": place, **({"km": float(km)} if km else {})}
            if f.get("district") and f["district"].lower() in place.lower():
                del f["district"]
    if re.search(r"\barchive[ds]?\b|\bhistoric(?:al)?\b|\bold\s+projects?\b", q, re.I):
        f["source"] = "archive"
    elif re.search(r"\blive\s+projects?\b|\bin\s+execution\b|\bcurrent\s+projects?\b", q, re.I):
        f["source"] = "live"
    return f


def _refs(q: str, vocab: Vocabulary) -> list[str]:
    refs = [m.group(0).upper() for m in _RE_LIVE_REF.finditer(q)]
    refs += [f"ARC-{m.group(1)}" for m in _RE_ARC_REF.finditer(q)]
    refs += [t for t in _RE_TOKEN_REF.findall(q) if t in vocab.archive_refs and t not in refs]
    return list(dict.fromkeys(refs))


def plan(db: Session, question: str, viewer, context: Optional[dict], today: date) -> dict:
    """{intent, tool_calls: [{tool, args}], clarification?} — the same shape as the
    model planner's output."""
    q = question.strip()
    ctx = context or {}
    vocab = vocabulary(db)
    refs = _refs(q, vocab)
    calls: list[dict] = []

    def done(intent: str, clarification: Optional[str] = None) -> dict:
        out = {"intent": intent, "tool_calls": calls[:3]}
        if clarification:
            out["clarification"] = clarification
        return out

    if re.search(r"\b(?:grid\s*nova(?:'s)?\s+(?:office|address|contact|phone|email|leadership|partners?|track\s+record|"
                 r"gstin|bank)|head\s+office|branch\s+office|company\s+(?:facts|details|address)|"
                 r"standard\s+warranty|payment\s+schedule\s+terms|commercial\s+terms)\b", q, re.I):
        calls.append({"tool": "company_reference", "args": {}})
        return done("company_reference")

    associates = [m.group(0).upper() for m in _RE_ASSOCIATE.finditer(q)]
    if associates and not re.search(r"\bhow\s+many\b|\bprojects?\b", q, re.I):
        calls += [{"tool": "associate_summary", "args": {"associate_id": a}} for a in associates]
        return done("associate_summary")

    for ref in refs[:3]:
        calls.append({"tool": "project_dossier", "args": {"ref": ref}})
    if refs:
        return done("project_dossier")

    lead_ids = [int(m.group(1)) for m in _RE_LEAD.finditer(q)]
    if lead_ids:
        calls += [{"tool": "lead_lookup", "args": {"lead_id": i}} for i in lead_ids[:3]]
        return done("lead_lookup")

    if re.search(r"\bpipeline\b|\bleads?\s+(?:by|per)\s+stage\b|\bhow\s+many\s+leads?\b", q, re.I):
        args = {}
        period = _period(q, today)
        if period:
            args = {"created_from": period[0].isoformat(), "created_to": period[1].isoformat()}
        calls.append({"tool": "pipeline_summary", "args": args})
        return done("pipeline_summary")

    if re.search(r"\b(?:service\s+requests?|complaints?|tickets?|o&m\s+requests?)\b", q, re.I):
        args = {"status": "open_or_in_progress"} if re.search(r"\bopen\b|\bpending\b|\bunresolved\b", q, re.I) else {}
        calls.append({"tool": "service_requests", "args": args})
        return done("service_requests")

    if re.search(r"\b(?:approved\s+)?(?:makes?|brands?)\b.*\b(?:package|nova\s+assured|nova\s+signature|approved)\b|"
                 r"\b(?:nova\s+assured|nova\s+signature)\b|\bcatalogue\b|\bequipment\s+master\b", q, re.I) \
            and not re.search(r"\bprojects?\b|\binstalled\b|\bcommissioned\b", q, re.I):
        args = {}
        if m := re.search(r"\bnova\s+(assured|signature)\b", q, re.I):
            args["package"] = f"NOVA_{m.group(1).upper()}"
        if re.search(r"\binverters?\b", q, re.I):
            args["component"] = "inverter"
        elif re.search(r"\b(?:panels?|modules?)\b", q, re.I):
            args["component"] = "solar_panel"
        kw = _RE_KW_EXACT.search(q)
        phase = re.search(r"\b(single|three|1|3)[\s-]*(?:phase|ph)\b", q, re.I)
        if args.get("package") and kw and phase and re.search(r"\bprice|\bcost|\bhow\s+much\b", q, re.I):
            args.pop("component", None)
            args.update({"kw": int(float(kw.group(1))),
                         "phase": "single_phase" if phase.group(1).lower() in ("single", "1") else "three_phase"})
        calls.append({"tool": "catalogue_lookup", "args": args})
        return done("catalogue_lookup")

    created = bool(re.search(r"\b(?:brought|onboarded|registered|signed\s+up|sourced|added|new\s+customers?|created)\b",
                             q, re.I))
    f = _filters(db, q, viewer, vocab, today, created=created)
    if ctx.get("project_ref") and not f and not re.search(r"\bhow\s+many\b|\btotal\b", q, re.I):
        calls.append({"tool": "project_dossier", "args": {"ref": ctx["project_ref"]}})
        return done("project_dossier")
    if ctx.get("lead_id") and not f:
        calls.append({"tool": "lead_lookup", "args": {"lead_id": ctx["lead_id"]}})
        return done("lead_lookup")

    memory_words = re.search(r"\b(?:remember|said|says|told|prefer(?:s|ence)?|objections?|concerns?|worried|budget|"
                             r"history\s+with|notes?\s+on|conversation|what\s+do\s+we\s+know)\b", q, re.I)
    if access.reads_customer_memory(viewer) and (ctx.get("customer_id") or (memory_words and f.get("customer_name"))):
        args = {"question": q[:1000]}
        if ctx.get("customer_id"):
            args["customer_id"] = ctx["customer_id"]
        else:
            args["name"] = f["customer_name"]
        calls.append({"tool": "customer_records", "args": args})
        return done("customer_records")

    money_total = re.search(r"\b(?:how\s+much|total|sum|amount)\b", q, re.I)
    if re.search(r"\boutstanding\b|\bdue\b|\bbalance\b|\bunpaid\b|\bpending\s+payments?\b|\bnot\s+(?:yet\s+)?paid\b",
                 q, re.I):
        f["source"] = "live"
        if money_total:
            calls.append({"tool": "aggregate", "args": {"metric": "sum_outstanding", "filters": f}})
        calls.append({"tool": "payments_status", "args": {"filters": f, "only_outstanding": True}})
        return done("payments_status")
    if re.search(r"\breceived\b|\bcollected\b|\bpaid\s+so\s+far\b", q, re.I) and money_total:
        calls.append({"tool": "aggregate", "args": {"metric": "sum_received", "filters": {**f, "source": "live"}}})
        return done("aggregate")

    if re.search(r"\bwarrant(?:y|ies)\b", q, re.I) and re.search(r"\bexpir|\bend(?:s|ing)?\b|\bdue\b|\blapse", q, re.I) \
            and not f.get("customer_name"):
        calls.append({"tool": "warranty_expiring", "args": {"days": _days(q), "filters": f}})
        return done("warranty_expiring")

    group = next((g for pattern, g in GROUP_WORDS if re.search(pattern, q, re.I)), None)
    metric = None
    if re.search(r"\bhow\s+many\b|\bnumber\s+of\b|\bcount\b|\bmost\b", q, re.I):
        metric = "count"
    if re.search(r"\b(?:total|sum|combined|overall)\b.{0,40}\b(?:capacity|kw|size|mw)\b|"
                 r"\b(?:capacity|kw|mw)\b.{0,20}\b(?:total|in\s+all|altogether)\b|\bhow\s+much\s+capacity\b", q, re.I):
        metric = "sum_capacity_kw"
    elif re.search(r"\baverage\b|\bavg\b|\bmean\b", q, re.I) and re.search(r"\b(?:size|capacity|kw)\b", q, re.I):
        metric = "avg_capacity_kw"
    elif re.search(r"\b(?:total|sum|how\s+much)\b.{0,40}\b(?:contract|value|revenue|sales|business|order\s+value)\b",
                   q, re.I):
        metric = "sum_contract_value"
    if group == "associate" and metric is None:
        metric = "count"
    if metric:
        if "near" in f:
            calls.append({"tool": "search_projects", "args": {"filters": f, "limit": 50}})
            return done("search_projects")
        args = {"metric": metric, "filters": f}
        if group:
            args["group_by"] = group
            if group == "associate":
                args["limit"] = 5
                f.setdefault("source", "live")
        calls.append({"tool": "aggregate", "args": args})
        return done("aggregate")

    if not f:
        return done("unclear", "I need something to search on — a project reference (GNE-… or ARC-…), a lead "
                               "number, a customer name, a district or state, a make, a size or a year.")
    calls.append({"tool": "search_projects", "args": {"filters": f, "limit": 20}})
    return done("search_projects")

