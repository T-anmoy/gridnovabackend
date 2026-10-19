"""Everything Nova Records says that is NOT written by the model (BE-9 §5, §6, §9):

  * evidence text — tool results as "key: value" lines, the form app/agents/grounding.py
    checks an answer against; customer-written free text is kept apart (quoted, data)
  * the deterministic templated answer — used in mock mode and whenever a generated
    answer fails verification twice
  * tables — built straight from tool rows (the model never writes a table)
  * citations, the "what I searched" list, not-on-record sentences, suggestions
"""
from __future__ import annotations

import re
from typing import Optional

from app.records import access

UNTRUSTED_KEYS = {"notes", "om_status", "address_text", "battery_details", "other_equipment", "description",
                  "documents", "data_quality_flags"}
REF_KEYS = {"ref", "project_ref", "customer_ref"}
MAX_TEMPLATE_ITEMS = 5

FILTER_PHRASES = {
    "text": "text “{}”", "ref": "reference {}", "customer_name": "customer name “{}”", "state": "state {}",
    "district": "district {}", "block": "block {}", "scheme": "scheme {}", "stage": "stage {} reached",
    "capacity_min_kw": "at least {} kW", "capacity_max_kw": "at most {} kW", "commissioned_from": "commissioned from {}",
    "commissioned_to": "commissioned to {}", "created_from": "created from {}", "created_to": "created to {}",
    "module_make": "module make {}", "inverter_make": "inverter make {}", "associate": "associate {}",
    "source": "{} projects only", "near_place": "near “{}”", "radius_km": "within {} km", "lead_id": "lead {}",
    "status": "status {}", "days": "next {} days", "warranty_ends_from": "warranty ending from {}",
    "warranty_ends_to": "to {}", "only_outstanding": None, "metric": None, "group_by": "grouped by {}",
    "anchor_projects": None, "candidates_measured": None, "candidates_capped": None, "assigned": "assigned {}",
    "associate_id": "associate {}", "name": "name “{}”", "make": "make {}", "component": "component {}",
    "package": "package {}", "customer_id": "customer C{}", "within_warranty": "within warranty: {}",
}
METRIC_PHRASES = {"count": "Projects", "sum_capacity_kw": "Total capacity", "avg_capacity_kw": "Average capacity",
                  "sum_contract_value": "Total contract value", "sum_received": "Total received",
                  "sum_outstanding": "Total outstanding", "lead_count": "Leads"}
TOOL_TITLES = {"search_projects": "Projects", "warranty_expiring": "Warranties ending", "lead_lookup": "Leads",
               "payments_status": "Payments", "service_requests": "Service requests", "associate_summary": "Associates",
               "catalogue_lookup": "Approved makes and equipment", "customer_records": "Customers"}


def _scalar(v) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


_RE_ISO_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?\+00:00$")


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, str) and (m := _RE_ISO_TIME.match(v)):
        return f"{m.group(1)} {m.group(2)} UTC"   # a date the verifier can read
    return "not on record" if v is None or v == "" else str(v)


# ── "What I searched" ───────────────────────────────────────────────────────────

def searched_phrase(searched: dict) -> str:
    parts = []
    for key, value in searched.items():
        if key == "near" and isinstance(value, dict):
            if value.get("place"):
                continue   # reported through near_place
            parts.append(f"within {value.get('km') or 'the default'} km of {value.get('lat')}, {value.get('lng')}")
            continue
        template = FILTER_PHRASES.get(key, key.replace("_", " ") + " {}")
        if template is None or value is None or value is False:
            continue
        parts.append(template.format(value))
    return ", ".join(parts) or "everything you can see"


def searched_list(results: list) -> list[dict]:
    return [{"tool": r["tool"], "filters": r["searched"], "summary": searched_phrase(r["searched"])} for r in results]


# ── Evidence ─────────────────────────────────────────────────────────────────────

def _line(ref: Optional[str], data: dict) -> str:
    body = "; ".join(f"{k}: {_fmt(v)}" for k, v in data.items() if k not in UNTRUSTED_KEYS and k != "ref"
                     and _scalar(v))
    return f"[{ref}] {body}" if ref else body


def evidence(results: list) -> str:
    """The facts an answer may state: every tool result as `key: value` lines, without
    customer-written free text."""
    lines = []
    for r in results:
        lines.append(f"=== {r['tool']} — searched: {searched_phrase(r['searched'])} ===")
        if r.get("total") is not None:
            lines.append(f"matching_total: {r['total']}")
        kind = r["kind"]
        if kind == "rows":
            lines += [_line(row.get("ref"), row) for row in r["rows"]]
            if r.get("aggregate"):
                lines.append(_line(None, r["aggregate"]))
            if r.get("record"):
                lines.append(_line(r["record"].get("ref"), r["record"]))
        elif kind == "aggregate":
            a = r["aggregate"]
            lines.append(f"{a['metric']}: {_fmt(a['total'])} (projects: {a['projects']})")
            lines += [f"group {_fmt(g['group'])} — {a['metric']}: {_fmt(g['value'])} (projects: {g['projects']})"
                      + (f"; associate_name: {g['associate_name']}" if g.get("associate_name") else "")
                      for g in a["groups"]]
        elif kind == "candidates":
            lines += [_line(c["ref"], c) for c in r["candidates"]]
        elif kind == "record" and r["record"]:
            rec = r["record"]
            if r["tool"] == "project_dossier":
                lines.append(_line(rec["ref"], {"source": rec["source"], **rec["fields"]}))
                for section, items in rec["sections"].items():
                    for item in items:
                        if isinstance(item, dict):
                            lines.append(f"[{rec['ref']}] {section}: " + _line(None, item))
            elif r["tool"] == "customer_records":
                lines.append(rec["context"])
            elif r["tool"] == "company_reference":
                lines.append(rec["facts"])
            else:
                lines.append(_line(rec.get("ref"), rec))
        lines += [f"note: {n}" for n in r.get("notes") or []]
    return "\n".join(lines)


def untrusted_block(results: list) -> str:
    items = [u for r in results for u in r.get("untrusted") or []]
    if not items:
        return ""
    return "\n".join(f"[{u['ref']}] {u['field']}: «{u['text']}»" for u in items)


def computed_values(results: list) -> tuple[set, set]:
    """(totals and averages a tool computed, counts a tool returned) as floats."""
    computed, counts = set(), set()

    def num(v):
        try:
            return float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return None
    for r in results:
        if r.get("total") is not None:
            counts.add(float(r["total"]))
        if r["kind"] == "rows":
            counts.add(float(len(r["rows"])))
            for key, value in (r.get("aggregate") or {}).items():
                if (n := num(value)) is not None:
                    (counts if key == "projects" else computed).add(n)
        if r["kind"] == "aggregate":
            a = r["aggregate"]
            counts.add(float(a["projects"]))
            if (n := num(a["total"])) is not None:
                computed.add(n)
                if a["metric"] in ("count", "lead_count"):
                    counts.add(n)
            for g in a["groups"]:
                counts.add(float(g["projects"]))
                if (n := num(g["value"])) is not None:
                    computed.add(n)
                    if a["metric"] in ("count", "lead_count"):
                        counts.add(n)
        if r["kind"] == "candidates":
            counts.add(float(len(r["candidates"])))
    return computed, counts


# Tools that show a role money or contact details its project view (config/archive.yaml)
# does not: customer memory and leads follow /memory/ask and the lead pages (a Sales rep
# sees their own customers' proposals and phone numbers there), and the list price is shown
# to the roles that see /pricing/breakdown. Anything else is a hidden field.
MEMORY_TOOLS = ("customer_records", "lead_lookup")


def money_visible(viewer, results: list) -> bool:
    return access.sees_money(viewer) or any(
        r["tool"] in MEMORY_TOOLS or (r["tool"] == "catalogue_lookup" and r.get("record")) for r in results)


def contact_visible(viewer, results: list) -> bool:
    return access.sees(viewer, "customer_phone") or any(r["tool"] in MEMORY_TOOLS for r in results)


def is_empty(r: dict) -> bool:
    kind = r["kind"]
    if kind == "rows":
        return not r["rows"] and not r.get("record")
    if kind == "aggregate":
        return r["aggregate"]["projects"] == 0
    if kind == "candidates":
        return not r["candidates"]
    return not r.get("record")


# ── Templated answers ────────────────────────────────────────────────────────────

def _s(n) -> str:
    return "" if str(n) in ("1", "1.0") else "s"


def _project_line(row: dict) -> str:
    bits = []
    if row.get("capacity_kw"):
        bits.append(f"{row['capacity_kw']} kW")
    if row.get("scheme_or_segment"):
        bits.append(str(row["scheme_or_segment"]))
    place = ", ".join(str(row[k]) for k in ("block", "district", "state") if row.get(k))
    if place:
        bits.append(place)
    if row.get("customer_name"):
        bits.append(f"customer {row['customer_name']}")
    if row.get("commissioning_date"):
        bits.append(f"commissioned {row['commissioning_date']}")
    elif row.get("current_stage"):
        bits.append(f"stage {row['current_stage']}")
    if row.get("module_make"):
        bits.append(f"modules {row['module_make']}")
    if row.get("inverter_make"):
        bits.append(f"inverter {row['inverter_make']}")
    for key, label in (("warranty_end_modules", "module warranty ends"), ("warranty_end_inverter",
                                                                          "inverter warranty ends")):
        if row.get(key):
            bits.append(f"{label} {row[key]}")
    if row.get("distance_km") is not None:
        bits.append(f"{row['distance_km']} km away")
    return f"[{row['ref']}] " + "; ".join(bits)


def _money(v) -> str:
    return f"₹{v}" if v is not None else "not on record"


def _rows_answer(r: dict) -> str:
    tool, rows, total = r["tool"], r["rows"], r.get("total", len(r["rows"]))
    phrase = searched_phrase(r["searched"])
    noun = {"lead_lookup": "lead", "service_requests": "service request", "associate_summary": "associate",
            "catalogue_lookup": "catalogue entry"}.get(tool, "project")
    if tool == "catalogue_lookup":
        lines = [f"Approved makes on record ({phrase}):"]
        for row in rows[:8]:
            what = f"{row['package']} {row['component']}" if row.get("package") else \
                f"equipment master {row['component']}"
            lines.append(f"[{row['ref']}] {what}: {row['makes']}" + (f" {row['model']}" if row.get("model") else "")
                         + (" (or equivalent)" if row.get("or_equivalent") else ""))
        if r.get("record"):
            rec = r["record"]
            lines.append(f"[{rec['ref']}] {rec['package']} {rec['kw']} kW {rec['phase'].replace('_', ' ')} list price: "
                         f"basic ₹{rec['basic_inr']}, GST ₹{rec['gst_inr']}, final ₹{rec['final_price_inr']}")
        return "\n".join(lines)
    head = f"Found {total} {noun}{_s(total)} matching {phrase}."
    if tool == "payments_status" and r.get("aggregate"):
        a = r["aggregate"]
        head = (f"Across {a['projects']} live project{_s(a['projects'])} matching {phrase}: contract value "
                f"{_money(a['total_contract_value'])}, received {_money(a['total_received'])}, outstanding "
                f"{_money(a['total_outstanding'])}.")
    lines = [head]
    if r.get("truncated"):
        lines[0] += f" Showing the first {len(rows)}."
    for row in rows[:MAX_TEMPLATE_ITEMS]:
        if tool in ("search_projects", "warranty_expiring"):
            lines.append(_project_line(row))
        elif tool == "payments_status":
            lines.append(f"[{row['ref']}] contract {_money(row['contract_value_inr'])}, received "
                         f"{_money(row['amount_received_inr'])}, outstanding {_money(row['amount_outstanding_inr'])}"
                         + (f" — due: {row['milestones_due']}" if row.get("milestones_due") else ""))
        elif tool == "lead_lookup":
            who = f", {row['customer_name']}" if row.get("customer_name") else ""
            project = f", project [{row['project_ref']}]" if row.get("project_ref") else ""
            lines.append(f"[{row['ref']}] stage {row['pipeline_stage']}{who}{project}, opened {row['created_at'][:10]}")
        elif tool == "service_requests":
            lines.append(f"[{row['ref']}] {row['category']}, {row['status']}, on [{row['project_ref']}], raised "
                         f"{(row['raised_on'] or '')[:10]}")
        elif tool == "associate_summary":
            extra = "; ".join(f"{k.replace('_inr', '').replace('_', ' ')} ₹{v}" for k, v in row.items()
                              if k.startswith("commission_"))
            lines.append(f"[{row['ref']}] {row['name']} — {row['type']}, {row['status']}, "
                         f"{row['district'] or 'district not on record'}; customers onboarded "
                         f"{row['customers_onboarded']}, verified {row['customers_verified']}"
                         + (f"; {extra}" if extra else ""))
    if len(rows) > MAX_TEMPLATE_ITEMS:
        lines.append("The table has the rest.")
    return "\n".join(lines)


def _aggregate_answer(r: dict, viewer) -> str:
    a = r["aggregate"]
    phrase = searched_phrase({k: v for k, v in r["searched"].items() if k not in ("metric", "group_by")})
    unit = " kW" if "capacity" in a["metric"] else ""
    money = a["metric"] in ("sum_contract_value", "sum_received", "sum_outstanding")
    value = _money(a["total"]) if money else f"{_fmt(a['total'])}{unit}"
    if a["metric"] in ("count", "lead_count"):
        noun = "lead" if a["metric"] == "lead_count" else "project"
        lines = [f"{a['projects']} {noun}{_s(a['projects'])} match {phrase}."]
    else:
        lines = [f"{METRIC_PHRASES[a['metric']]} for {a['projects']} project{_s(a['projects'])} matching {phrase}: "
                 f"{value}."]
    if a["groups"]:
        lines.append(f"By {a['group_by'].replace('_', ' ')}:")
        for g in a["groups"][:MAX_TEMPLATE_ITEMS]:
            label = (f"[{g['group']}] {g['associate_name']}" if a["group_by"] == "associate" and g["group"]
                     else _fmt(g["group"]))
            shown = _money(g["value"]) if money else f"{_fmt(g['value'])}{unit}"
            if a["metric"] in ("count", "lead_count"):
                lines.append(f"- {label}: {g['projects']}")
            else:
                lines.append(f"- {label}: {shown} ({g['projects']} project{_s(g['projects'])})")
    lines += list(r.get("notes") or [])
    return "\n".join(lines)


def _dossier_answer(r: dict) -> str:
    rec = r["record"]
    f, ref = rec["fields"], rec["ref"]
    lines = [_project_line({"ref": ref, **{k: f.get(k) for k in ("capacity_kw", "scheme_or_segment", "block",
                                                                  "district", "state", "customer_name",
                                                                  "commissioning_date")},
                            "current_stage": f.get("current_stage")})]
    for item in rec["sections"].get("equipment", []):
        what = " ".join(str(item[k]) for k in ("make", "model") if item.get(k)) or "make not on record"
        lines.append(f"[{ref}] {item.get('category')}: {what}, {item.get('count')} unit{_s(item.get('count'))}"
                     + (f", rating {item['rating']}" if item.get("rating") else ""))
    for item in rec["sections"].get("warranty", []):
        ends = [f"{label} {item[key]}" for key, label in (("module_warranty_end", "modules"),
                                                          ("inverter_warranty_end", "inverter"),
                                                          ("system_warranty_end", "system")) if item.get(key)]
        if ends:
            lines.append(f"[{ref}] warranty ends: " + ", ".join(ends))
    if "contract_value_inr" in f:
        money = [f"contract {_money(f.get('contract_value_inr'))}"]
        if f.get("amount_received_inr") is not None:
            money.append(f"received {_money(f['amount_received_inr'])}")
        if f.get("amount_outstanding_inr") is not None:
            money.append(f"outstanding {_money(f['amount_outstanding_inr'])}")
        lines.append(f"[{ref}] " + ", ".join(money))
    if f.get("open_service_requests"):
        lines.append(f"[{ref}] open service requests: {f['open_service_requests']}")
    return "\n".join(lines)


def template_answer(results: list, viewer) -> str:
    parts = []
    for r in results:
        if is_empty(r):
            parts.append(not_on_record_sentence([r]))
            continue
        kind, tool = r["kind"], r["tool"]
        if kind == "rows":
            parts.append(_rows_answer(r))
        elif kind == "aggregate":
            parts.append(_aggregate_answer(r, viewer))
        elif tool == "project_dossier":
            parts.append(_dossier_answer(r))
        elif tool == "customer_records":
            rec = r["record"]
            body = "\n".join(rec["context"].splitlines()[:14])
            parts.append(f"Records found for {rec['name'] or 'this customer'} [{rec['customer_ref']}]:\n{body}")
        elif tool == "company_reference":
            parts.append("Grid Nova's verified company facts [COMPANY]:\n" + "\n".join(
                r["record"]["facts"].splitlines()[:12]))
    return "\n\n".join(p for p in parts if p)


def not_on_record_sentence(results: list) -> str:
    phrases = [searched_phrase(r["searched"]) for r in results] or ["that question"]
    return (f"Nothing on record that you can see matches {'; '.join(phrases)}. "
            "Try a broader search — drop a filter, widen the dates, or search live and archived projects together.")


def clarification_for(candidates: list) -> str:
    names = "; ".join(f"{c.get('name') or c.get('customer_name') or 'unnamed'} [{c['ref']}]" for c in candidates[:8])
    return f"More than one record matches that: {names}. Which one did you mean?"


# ── Tables, citations, suggestions ───────────────────────────────────────────────

def _column_type(key: str, sample) -> str:
    if key in REF_KEYS:
        return "ref"
    if isinstance(sample, bool):
        return "boolean"
    if key.endswith("_inr"):
        return "money"
    if "kw" in key:
        return "kw"
    if "date" in key or key.endswith(("_on", "_at")) or key.startswith("warranty_end"):
        return "date"
    if key in ("distance_km", "projects", "count", "open_service_requests", "module_count", "inverter_count"):
        return "number"
    return "text"


LABELS = {"ref": "Reference", "source": "Source", "capacity_kw": "Capacity (kW)", "scheme_or_segment": "Scheme",
          "system_type": "System", "state": "State", "district": "District", "block": "Block",
          "commissioning_date": "Commissioned", "module_make": "Module make", "inverter_make": "Inverter make",
          "customer_name": "Customer", "current_stage": "Stage", "distance_km": "Distance (km)",
          "warranty_end_modules": "Module warranty ends", "warranty_end_inverter": "Inverter warranty ends",
          "group": "Group", "value": "Value", "projects": "Projects", "associate_name": "Associate"}


def _table(title: str, rows: list, money_value: bool = False) -> Optional[dict]:
    if not rows:
        return None
    keys: list = []
    for row in rows:
        for k, v in row.items():
            if k not in keys and k not in UNTRUSTED_KEYS and _scalar(v) and not k.startswith("pump_") \
                    and k != "street_lamp_count":
                keys.append(k)
    columns = []
    for k in keys:
        sample = next((row.get(k) for row in rows if row.get(k) is not None), None)
        kind = "money" if (money_value and k == "value") else _column_type(k, sample)
        columns.append({"key": k, "label": LABELS.get(k, k.replace("_inr", " (₹)").replace("_", " ").capitalize()),
                        "type": kind})
    return {"title": title, "columns": columns, "rows": [{k: row.get(k) for k in keys} for row in rows]}


def tables(results: list) -> list[dict]:
    out = []
    for r in results:
        if is_empty(r):
            continue
        if r["kind"] == "rows":
            t = _table(TOOL_TITLES.get(r["tool"], "Results"), r["rows"])
        elif r["kind"] == "aggregate":
            a = r["aggregate"]
            money = a["metric"] in ("sum_contract_value", "sum_received", "sum_outstanding")
            groups = a["groups"] or [{"group": "All", "value": a["total"], "projects": a["projects"]}]
            t = _table(f"{METRIC_PHRASES.get(a['metric'], a['metric'])}"
                       + (f" by {a['group_by'].replace('_', ' ')}" if a["group_by"] else ""), groups, money)
        elif r["kind"] == "candidates":
            t = _table("Matching customers", r["candidates"])
        elif r["tool"] == "project_dossier":
            rec = r["record"]
            out.append({"title": f"Project {rec['ref']}", "columns": [
                {"key": "field", "label": "Field", "type": "text"}, {"key": "value", "label": "Value", "type": "text"}],
                "rows": [{"field": k, "value": v} for k, v in rec["fields"].items()
                         if _scalar(v) and v is not None and k not in UNTRUSTED_KEYS]})
            for section in ("equipment", "stages", "warranty", "service_requests"):
                items = [i for i in rec["sections"].get(section, []) if isinstance(i, dict)]
                tbl = _table(f"{section.replace('_', ' ').capitalize()} — {rec['ref']}", items)
                if tbl:
                    out.append(tbl)
            continue
        else:
            t = None
        if t:
            out.append(t)
    return out


def citations(answer: str, results: list) -> list[dict]:
    from app.agents.grounding import record_citations

    by_ref = {}
    for r in results:
        for ref in r["refs"]:
            by_ref.setdefault(ref["ref"].upper(), ref)
    cited = [m.upper() for m in record_citations(answer)]
    ordered = sorted(set(cited), key=lambda m: answer.upper().find(f"[{m}]"))
    return [{"ref": by_ref[m]["ref"], "kind": by_ref[m]["kind"], "label": by_ref[m]["label"],
             "path": by_ref[m].get("path")} for m in ordered if m in by_ref]


def suggestions(results: list, viewer, status: str) -> list[dict]:
    out: list[dict] = []

    def add(label, question=None, path=None):
        if question is None and path is None:
            return   # a link to nowhere is not a suggestion
        if len(out) < 3 and not any(s["label"] == label for s in out):
            out.append({"label": label, "question": question, "path": path})
    if status == "not_on_record":
        add("Search live and archived projects", "Search all projects, live and archived, without the date filter")
        add("Show totals by district", "How many projects do we have by district?")
        return out
    for r in results:
        if is_empty(r):
            continue
        if r["tool"] == "project_dossier":
            rec, ref = r["record"], r["refs"][0]
            add("Open project", path=ref.get("path"))
            add("Show warranty items", f"When do the warranties end for {rec['ref']}?")
            if access.reads_payments(viewer) and rec["source"] == "live":
                add("Payments for this project", f"What is outstanding on {rec['ref']}?")
        elif r["tool"] in ("search_projects", "warranty_expiring") and r["rows"]:
            first = r["rows"][0]
            add("Open the first result", path=r["refs"][0].get("path") if r["refs"] else None)
            add("Details of the first result", f"Show me everything on record for {first['ref']}")
            add("Totals by district", "How many of these projects are there by district?")
        elif r["kind"] == "aggregate":
            if r["aggregate"]["group_by"] != "district":
                add("Break down by district", "Break that down by district")
            if r["aggregate"]["group_by"] != "year":
                add("Break down by year", "Break that down by year")
        elif r["tool"] == "lead_lookup" and r["refs"]:
            add("Open lead", path=r["refs"][0].get("path"))
        elif r["tool"] == "customer_records":
            lead = next((ref for ref in r["refs"] if ref["kind"] == "lead"), None)
            add("Open lead", path=lead.get("path") if lead else None)
    return out
