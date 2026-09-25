"""The two project-type vocabularies, and the only mappings between them that hold
without guessing.

  * Lead.collected_fields["project_type"] — what the proposal engine matches on
    (config/proposal_content.yaml parameter_rules/pricing, and the chat extractor's
    instruction in app/llm/client.py): rooftop / ground_mounted / solar_pump.
  * ProjectFamily — the site-assessment form's and Project's enum.

government_institutional, street_lighting, om_service and consulting have no
equivalent project_type, so they deliberately map to nothing: a lead whose site
assessment names one of them stays missing project_type until a person sets it.
"""
from __future__ import annotations

from typing import Optional

from app.memory.models import ProjectFamily

PROJECT_TYPES = ("rooftop", "ground_mounted", "solar_pump")

FAMILY_TO_PROJECT_TYPE: dict[ProjectFamily, str] = {
    ProjectFamily.rooftop_solar: "rooftop",
    ProjectFamily.ground_mounted: "ground_mounted",
    # The water_pump family is solar water pumping (the PM-KUSUM schemes on the same
    # form); the chat vocabulary calls the same thing solar_pump.
    ProjectFamily.water_pump: "solar_pump",
}
PROJECT_TYPE_TO_FAMILY: dict[str, ProjectFamily] = {v: k for k, v in FAMILY_TO_PROJECT_TYPE.items()}


def project_type_for_family(family) -> Optional[str]:
    if family is None:
        return None
    return FAMILY_TO_PROJECT_TYPE.get(ProjectFamily(family))


def family_for_project_type(project_type: Optional[str]) -> Optional[ProjectFamily]:
    return PROJECT_TYPE_TO_FAMILY.get(project_type) if project_type else None
