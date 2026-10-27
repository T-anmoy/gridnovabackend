"""The optional "suggest from notes" assistant (BE-18 §11), off by default
(STUDIO_AI_FILL_ENABLED=false: no model call at all).

The model (LLM_MODEL) may only suggest the customer / site / connection fields listed in
config/studio.yaml `assistant_fields`. Its reply is parsed as JSON, every key outside that
list is dropped, and each value must pass the same validation as a typed value. Nothing is
saved: each suggestion comes back marked "check" for staff to accept field by field. It never
touches a price, the BOQ, items, terms or anything printed as a figure.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from app.proposals import document as d
from app.studio import config as studio_config

FORBIDDEN = ("price", "boq", "items", "payment_terms", "customer_scope", "notes", "notes_free", "plant", "savings",
             "display", "validity_days", "project", "system", "pm_surya_ghar", "cover_highlight")
_MODELS = {"addressee": d.Addressee, "site": d.Site, "connection": d.Connection}


def enabled() -> bool:
    from app.config import settings
    return bool(settings.studio_ai_fill_enabled)


def _parse(text: str) -> Optional[dict]:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def suggest(notes: str) -> dict:
    """{"suggestions": [{section, field, value, status: "check"}], "dropped": [...], "model": …}."""
    from app.config import settings
    from app.llm.client import llm_client
    allowed = studio_config.load()["assistant_fields"]
    reply = llm_client.studio_suggest(text=notes, fields={s: list(f) for s, f in allowed.items()})
    if reply is None:
        return {"suggestions": [], "dropped": [], "model": None, "available": False}
    raw = _parse(reply.get("text", "")) or {}
    suggestions, dropped = [], []
    for section, values in raw.items():
        if section not in allowed or not isinstance(values, dict):
            dropped.append(section)
            continue
        for field, value in values.items():
            if field not in allowed[section]:
                dropped.append(f"{section}.{field}")
                continue
            try:
                checked = _MODELS[section].model_validate({field: value})
            except ValueError:
                dropped.append(f"{section}.{field}")
                continue
            clean = getattr(checked, field)
            if clean in (None, "", []):
                continue
            suggestions.append({"section": section, "field": field,
                                "value": [str(x) for x in clean] if isinstance(clean, list) else
                                (str(clean) if not isinstance(clean, (bool, int)) else clean),
                                "status": "check"})
    suggestions = [s for s in suggestions if s["section"] not in FORBIDDEN]
    return {"suggestions": suggestions, "dropped": dropped, "model": reply.get("model") or settings.llm_model,
            "available": True}
