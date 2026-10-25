"""GET /api/v1/pricing/estimate-assumptions — the engine's assumptions and subsidy rules
as the website's "How we calculate" panel shows them: every figure with its status and
source. Built once per process from the two config files (themselves loaded once)."""
from __future__ import annotations

from functools import lru_cache

from app.estimate.config import estimate_config, subsidy_config, version
from app.estimate.engine import public


def _table(t) -> dict:
    return {"discom": t.discom, "category": t.category, "status": t.status, "source": t.source,
            "as_of": t.as_of, "effective_from": t.effective_from, "available": t.usable,
            "slabs": [{"up_to_units": s.up_to_units, "rate": s.rate} for s in t.slabs] if t.usable else None,
            "fixed_charge": {"type": t.fixed_charge.type, "value": t.fixed_charge.value}
            if t.usable and t.fixed_charge else None,
            "electricity_duty": {"type": t.electricity_duty.type, "value": t.electricity_duty.value}
            if t.usable and t.electricity_duty else None}


def _rule(rule: dict) -> dict:
    if rule["type"] == "capacity_steps":
        return {"type": rule["type"], "steps": [{"min_kw": k, "amount": a} for k, a in rule["steps"]]}
    return dict(rule)


@lru_cache(maxsize=1)
def public_body() -> dict:
    e, s = estimate_config(), subsidy_config()
    gen, sav, tar = e["generation"], e["savings"], e["tariffs"]
    cen = s["central"]
    credit = [{"state": k, "rule": v["rule"], **v["value"].public("export_credit")}
              for k, v in sav["export_credit"].items()]
    return public({
        "estimate_version": version(),
        "generation": {
            "units_per_kwp_per_month": [{"state": k, **v.public("units_per_kwp_per_month")}
                                        for k, v in gen["states"].items()]
            + [{"state": "other", **gen["fallback"].public("units_per_kwp_per_month")}],
            "annual_is_monthly_times": 12,
            "system_losses": "Included in the generation figure; no second derate is applied.",
            "shading": "Not modelled as a loss; a significant-shading report needs a site visit.",
            "degradation": [gen["first_year_percent"].public("degradation_first_year_percent"),
                            gen["annual_percent"].public("degradation_annual_percent")],
        },
        "roof_area": [e["roof_area"]["sqft_per_kwp"].public("sqft_per_kwp"),
                      e["roof_area"]["sqft_per_kwp_low"].public("sqft_per_kwp_low")],
        "sizing": {"target_offset": e["sizing"]["target_offset_percent"].public("target_offset_percent"),
                   "formula": "exact kW = monthly units × 12 × target offset ÷ (units per kWp per month × 12); "
                              "the smaller of that and roof area ÷ sq ft per kWp; priced at the next whole kW "
                              "in the phase range, with the whole kW below also shown when in range.",
                   "sanctioned_load": "A size above the sanctioned load is a warning, never a silent cap."},
        "savings": {"method": "marginal: bill(units) − bill(units − solar units), fixed charges unchanged",
                    "escalation": sav["escalation"].public("tariff_escalation_percent_per_year"),
                    "long_term_years": sav["years"].public("long_term_years"),
                    "export_credit": credit + [{"state": "other", "rule": sav["export_credit_fallback"]["rule"],
                                                **sav["export_credit_fallback"]["value"].public("export_credit")}],
                    "o_and_m": sav["o_and_m_first_year_free"].public("o_and_m_first_year_free"),
                    "payback": "net cost after confirmed subsidies ÷ year-1 annual saving"},
        "tariffs": {"flat_rate": tar["flat_rate"].public("flat_rate_inr_per_kwh"),
                    "implied_rate_sanity": {"min": tar["sanity"]["min"], "max": tar["sanity"]["max"],
                                            "unit": "INR/kWh", "status": tar["sanity"]["status"],
                                            "source": tar["sanity"]["source"], "as_of": tar["sanity"]["as_of"]},
                    "discoms": [{"state": d["state"], **_table(t)}
                                for d in tar["discoms"].values() for t in d["categories"].values()],
                    "free_units": [{"state": f["state"], "discoms": list(f["discoms"]),
                                    **f["value"].public("free_units_per_month")} for f in tar["free_units"].values()]},
        "confidence_rules": {"high": "Monthly units given and a confirmed DISCOM tariff.",
                             "medium": "The customer's own bill ÷ units, or a bill solved through a confirmed "
                                       "DISCOM tariff; or high with a generation figure not confirmed for the state.",
                             "low": "Grid Nova's flat ₹ per unit, or no consumption given (roof area only)."},
        "subsidies": {
            "central": {"scheme": cen["scheme"], "status": cen["status"], "source": cen["source"],
                        "as_of": cen["as_of"],
                        "residential": {cat: {"slabs": [{"up_to_kwp": u, "rate_per_kwp": r} for u, r in t["slabs"]],
                                              "max_inr": t["max_inr"]} for cat, t in cen["residential"].items()},
                        "housing_society": cen["housing_society"],
                        "special_category_states": list(cen["special_category_states"]),
                        "eligible_segments": list(cen["eligible_segments"]),
                        "eligible_connection_types": list(cen["eligible_connection_types"]),
                        "cfa_capacity_basis": cen["cfa_capacity_basis"].public("cfa_capacity_basis"),
                        "conditions": list(cen["conditions"])},
            "state_top_ups": [{"state": item["state"], "amount_rule": _rule(item["amount_rule"]),
                               "applies_to": item["applies_to"], "note": item["eligibility_note"],
                               "status": item["status"], "source": item["source"], "as_of": item["as_of"],
                               "valid_from": item["valid_from"], "valid_until": item["valid_until"]}
                              for items in s["state_top_ups"].values() for item in items],
            "deduction_rule": "Only confirmed subsidies are deducted from net cost. Announced state support is "
                              "shown separately and is not guaranteed; nothing unconfirmed is ever deducted.",
        },
    })
