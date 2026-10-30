"""BE-22 — launch content: drafted wording approved in the dashboard, the gates that hold
every document until it is, and pricing that can never show ₹0.

Every test here starts with nothing approved (launch_texts_unapproved) unless it
approves through the real route, so the gates are seen from the customer's side."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.memory.models import AuditLog, PipelineStage, PlaceholderResolution, ProposalStatus
from app.placeholders import registry
from app.proposals import content_engine, send_guard
from tests.conftest import admin_headers

pytestmark = [pytest.mark.launch_texts_unapproved, pytest.mark.usefixtures("known_employees")]

URL = "/api/v1/dashboard/placeholders"
DASH = "/api/v1/dashboard"
WORDING = ("PRO-2", "PRO-7", "PRO-8", "PRO-10", "PRO-11", "PRO-12", "ONB-5", "P6", "HW-OM")


def staff(role: str, n: int = 1) -> dict:
    return {"X-Employee-Id": f"emp-{role}-{n}", "X-Employee-Role": role}


def sha(entry_id: str) -> str:
    return registry.sha256(registry.current_text(registry.load().by_id[entry_id]))


def approve(client, entry_id: str, **body):
    legal = registry.load().by_id[entry_id].legal_review_required
    return client.post(f"{URL}/{entry_id}/approve", headers=staff("admin"),
                       json={"content_sha256": sha(entry_id), "legal_review_confirmed": legal, **body})


def item(client, entry_id: str) -> dict:
    body = client.get(URL, headers=staff("admin")).json()
    return next(i for a in body["areas"] for i in a["items"] if i["id"] == entry_id)


# ── The registry: approved_text ──────────────────────────────────────────────────

class TestApprovedTextProbe:
    def test_the_nine_texts_are_approved_text_with_legal_review_where_required(self):
        reg = registry.load()
        assert {e.id for e in registry.approved_text_entries()} == set(WORDING)
        assert {e.id for e in registry.approved_text_entries() if e.legal_review_required} == {"PRO-2", "ONB-5"}
        for eid in WORDING:
            entry = reg.by_id[eid]
            assert entry.needed_from_grid_nova.startswith(("Approve the drafted wording", "Have Grid Nova's legal"))
            assert not registry.has_marker(entry), eid           # drafted wording, no marker left

    def test_drafted_but_unapproved_is_open(self, db_session):
        entry = registry.load().by_id["PRO-7"]
        assert registry.evaluate(entry, db_session)["resolved"] is False
        assert registry.evaluate(entry)["resolved"] is None      # cannot tell without a database
        assert registry.text_is_approved("PRO-7", db_session) is False
        assert set(registry.drafted_not_approved(db_session)) == set(WORDING)

    def test_approval_resolves_and_a_text_change_lapses_it(self, client, db_session, monkeypatch):
        assert approve(client, "PRO-7").status_code == 200
        entry = registry.load().by_id["PRO-7"]
        assert registry.evaluate(entry, db_session)["resolved"] is True
        assert registry.text_is_approved("PRO-7", db_session) is True
        real = registry.current_text
        monkeypatch.setattr(registry, "current_text",
                            lambda e: real(e) + "\nA new promise." if e.id == "PRO-7" else real(e))
        assert registry.evaluate(entry, db_session)["resolved"] is False
        assert registry.text_is_approved("PRO-7", db_session) is False
        assert registry.approval_state(entry, db_session)["state"] == "lapsed"
        assert item(client, "PRO-7")["approval_state"] == "lapsed"

    def test_a_marker_is_never_approved(self, db_session, monkeypatch):
        assert registry.text_is_approved("PRO-7", db_session, text="[PLACEHOLDER: scope]") is False
        assert registry.text_is_approved("P6", db_session, text="heading: DUMMY — heading") is False

    def test_hash_normalises_line_endings_and_edges(self):
        assert registry.sha256("a\r\nb\n") == registry.sha256("  a\nb") == registry.sha256("a\rb")
        assert registry.sha256("a\nb") != registry.sha256("a b")

    def test_unknown_or_non_wording_entries_are_never_approved(self, db_session):
        assert registry.text_is_approved("NOPE", db_session) is False
        assert registry.text_is_approved("A1", db_session) is False

    def test_registry_validates_the_new_fields(self):
        base = {"id": "X", "area": "pricing", "location": {"file": "f.yaml", "key": "a"}, "description": "d",
                "dummy_value": 1, "needed_from_grid_nova": "q?", "blocking": "tunable"}
        with pytest.raises(registry.RegistryError, match="approved_text entries only"):
            registry._validate({"entries": [{**base, "probe": "yaml_contains_marker", "legal_review_required": True}]})
        with pytest.raises(registry.RegistryError, match="needs location.key"):
            registry._validate({"entries": [{**base, "probe": "approved_text", "location": {"env": ["A"]}}]})


# ── The routes ───────────────────────────────────────────────────────────────────

class TestApprovalRoutes:
    def test_listing_shows_the_text_hash_and_state(self, client):
        p6 = item(client, "P6")
        assert p6["probe"] == "approved_text" and p6["approval_state"] == "not_approved" and p6["approval"] is None
        assert p6["current_text"] == ("heading: PAYMENT RECEIPT\nfooter: This receipt confirms that Grid Nova "
                                      "Energy has received the payment shown above in its bank account. It is not "
                                      "a GST tax invoice; the tax invoice is issued separately. Grid Nova never "
                                      "accepts cash or payment to any individual. Questions: info@gridnova.in · "
                                      "+91 98747 74440")
        assert p6["current_sha256"] == sha("P6") and p6["legal_review_required"] is False
        assert item(client, "ONB-5")["legal_review_required"] is True

    def test_approve_records_who_when_what_and_audits(self, client, db_session):
        r = approve(client, "P6", note="Read in full.")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["resolved"] is True and body["approval_state"] == "approved"
        assert body["approval"]["by"] == "emp-admin-1" and body["approval"]["note"] == "Read in full."
        assert body["approval"]["content_sha256"] == sha("P6") and body["approval"]["at"]
        row = db_session.execute(select(PlaceholderResolution).where(PlaceholderResolution.entry_id == "P6")) \
            .scalar_one()
        assert (row.action, row.content_sha256, row.resolved_by) == ("approved", sha("P6"), "emp-admin-1")
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "placeholder")).scalar_one()
        assert (audit.entity_id, audit.action, audit.performed_by) == ("P6", "wording_approved", "emp-admin-1")
        assert audit.after_value["content_sha256"] == sha("P6")

    def test_approval_of_changed_text_is_refused(self, client):
        r = client.post(f"{URL}/P6/approve", headers=staff("admin"), json={"content_sha256": "0" * 64})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "text_changed"
        assert r.json()["detail"]["current_sha256"] == sha("P6")

    def test_approval_of_a_placeholder_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(registry, "has_marker", lambda e: True)
        r = approve(client, "PRO-7")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "still_placeholder"

    @pytest.mark.parametrize("entry_id", ["PRO-2", "ONB-5"])
    def test_legal_review_tick_is_required(self, client, db_session, entry_id):
        r = approve(client, entry_id, legal_review_confirmed=False)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "legal_review_required"
        assert registry.text_is_approved(entry_id, db_session) is False
        r = approve(client, entry_id, legal_review_confirmed=True)
        assert r.status_code == 200 and r.json()["approval"]["legal_review_confirmed"] is True

    @pytest.mark.parametrize("role", ["management", "sales", "engineer", "operations", "accounts", "proposal_desk"])
    def test_only_admin_approves_or_revokes(self, client, role):
        r = client.post(f"{URL}/P6/approve", headers=staff(role), json={"content_sha256": sha("P6")})
        assert r.status_code == 403
        assert client.post(f"{URL}/P6/revoke", headers=staff(role), json={"note": "x"}).status_code == 403

    def test_associates_cannot_reach_the_routes(self, client):
        r = client.post(f"{URL}/P6/approve", headers={"X-Associate-Id": "GNA-WB-26-00001"},
                        json={"content_sha256": sha("P6")})
        assert r.status_code == 401

    def test_revoke_unapproves_appends_and_audits(self, client, db_session):
        approve(client, "PRO-8")
        r = client.post(f"{URL}/PRO-8/revoke", headers=staff("admin"), json={"note": "Tender terms changed."})
        assert r.status_code == 200 and r.json()["approval_state"] == "not_approved"
        assert registry.text_is_approved("PRO-8", db_session) is False
        rows = db_session.execute(select(PlaceholderResolution.action).where(
            PlaceholderResolution.entry_id == "PRO-8").order_by(PlaceholderResolution.id)).scalars().all()
        assert rows == ["approved", "revoked"]
        actions = db_session.execute(select(AuditLog.action).where(AuditLog.entity_type == "placeholder")
                                     .order_by(AuditLog.id)).scalars().all()
        assert actions == ["wording_approved", "wording_revoked"]
        again = client.post(f"{URL}/PRO-8/revoke", headers=staff("admin"), json={"note": "again"})
        assert again.status_code == 409 and again.json()["detail"]["code"] == "not_approved"
        assert approve(client, "PRO-8").status_code == 200                 # approve → revoke → approve
        assert registry.text_is_approved("PRO-8", db_session) is True

    def test_revoke_needs_a_note(self, client):
        approve(client, "PRO-8")
        assert client.post(f"{URL}/PRO-8/revoke", headers=staff("admin"), json={"note": "  "}).status_code == 422

    def test_wrong_kind_of_entry(self, client):
        r = client.post(f"{URL}/A1/approve", headers=staff("admin"), json={"content_sha256": "0" * 64})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_approved_text"
        r = client.post(f"{URL}/P6/resolve", headers=staff("admin"), json={"note": "x"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_manual"
        assert client.post(f"{URL}/NOPE/approve", headers=staff("admin"),
                           json={"content_sha256": "0" * 64}).status_code == 404

    def test_rate_limited_per_admin(self, client, monkeypatch):
        from app.api.routes import placeholders_admin
        monkeypatch.setattr(placeholders_admin, "DECISION_LIMIT", 2)
        assert approve(client, "PRO-7").status_code == 200
        assert approve(client, "PRO-8").status_code == 200
        r = approve(client, "PRO-10")
        assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"


# ── Proposals ────────────────────────────────────────────────────────────────────

def _library(section_id: str) -> str:
    return content_engine.load_content_config()["sections_library"][section_id]


def _proposal(memory, db_session, sections, *, total=250_000, fallback=False, email="be22@example.test"):
    customer = memory.get_or_create_customer(email=email, name="Institution")
    db_session.commit()
    lead = memory.create_lead(customer_id=customer.id)
    memory.update_lead_fields(lead_id=lead.id, new_fields={"name": "Institution"}, performed_by="t",
                              stage=PipelineStage.qualified)
    proposal = memory.create_proposal_draft(lead_id=lead.id, content={"sections": sections}, total_value=total,
                                            requires_manager_signoff=False, performed_by="emp-1",
                                            used_fallback_template=fallback)
    db_session.commit()
    return proposal.id


def _government_sections():
    return [content_engine._with_source({"id": sid, "category": "scope" if sid.endswith("_scope") else "note",
                                         "body": _library(sid)})
            for sid in ("government_scope", "government_compliance_note")]


def _approve_and_send(client, pid):
    assert client.post(f"{DASH}/proposals/{pid}/approve", headers=admin_headers()).status_code == 200
    return client.post(f"{DASH}/proposals/{pid}/send", headers=admin_headers())


class TestProposalGates:
    def test_composed_sections_record_their_source_entry(self):
        cfg = content_engine.load_content_config()
        lead = SimpleNamespace(id=1, collected_fields={"project_type": "rooftop", "segment": "government",
                                                       "capacity_kw": 20, "location": "Patna"})
        content = content_engine.compose_content({"lead": lead, "customer": None}, cfg)
        sources = {s["id"]: s.get("source_entry") for s in content["sections"]}
        assert sources["government_scope"] == "PRO-8" and sources["government_compliance_note"] == "PRO-11"
        assert sources["standard_terms"] == "PRO-2" and sources["cover_letter_intro"] is None

    def test_unapproved_sections_hold_the_send(self, client, memory, db_session):
        pid = _proposal(memory, db_session, _government_sections())
        r = _approve_and_send(client, pid)
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "unapproved_sections"
        assert detail["unapproved_sections"] == ["government_scope", "government_compliance_note"]
        assert "not yet approved" in detail["message"]
        db_session.expire_all()
        assert memory.get_proposal(pid).status == ProposalStatus.approved and memory.get_proposal(pid).date_sent is None

    def test_approved_sections_can_be_sent(self, client, memory, db_session):
        approve(client, "PRO-8")
        approve(client, "PRO-11")
        pid = _proposal(memory, db_session, _government_sections())
        assert _approve_and_send(client, pid).status_code == 200

    def test_a_section_drafted_with_older_wording_stays_held(self, client, memory, db_session):
        approve(client, "PRO-8")
        approve(client, "PRO-11")
        sections = _government_sections()
        sections[0]["body"] += "\nAn older sentence."
        pid = _proposal(memory, db_session, sections)
        assert _approve_and_send(client, pid).json()["detail"]["unapproved_sections"] == ["government_scope"]

    def test_a_section_without_source_entry_is_matched_by_id(self, db_session):
        sections = [{"id": "pm_kusum_note", "category": "note", "body": _library("pm_kusum_note")}]
        assert content_engine.unapproved_sections({"sections": sections}, db_session) == ["pm_kusum_note"]

    def test_generic_scope_is_held_for_manual_review_even_when_approved(self, client, memory, db_session):
        approve(client, "PRO-12")
        sections = [content_engine._with_source({"id": "fallback_generic_scope", "category": "scope",
                                                 "body": _library("fallback_generic_scope")})]
        pid = _proposal(memory, db_session, sections, fallback=True)
        r = _approve_and_send(client, pid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "manual_review_required"
        assert r.json()["detail"]["unapproved_sections"] == []

    def test_revocation_after_sending_closes_the_public_link(self, client, memory, db_session):
        approve(client, "PRO-8")
        approve(client, "PRO-11")
        pid = _proposal(memory, db_session, _government_sections())
        assert _approve_and_send(client, pid).status_code == 200
        db_session.expire_all()
        token = memory.get_proposal(pid).accept_token
        assert client.get(f"/api/v1/proposals/accept/{token}").status_code == 200
        client.post(f"{URL}/PRO-8/revoke", headers=staff("admin"), json={"note": "withdrawn"})
        for r in (client.get(f"/api/v1/proposals/accept/{token}"), client.post(f"/api/v1/proposals/accept/{token}"),
                  client.get(f"/api/v1/public/proposals/{token}/pdf", follow_redirects=False)):
            assert r.status_code == 409 and r.json()["detail"]["code"] == "proposal_not_available"
            assert "Government" not in r.text and "tender" not in r.text           # never the text itself
        db_session.expire_all()
        assert memory.get_proposal(pid).accepted_at is None

    def test_in_person_acceptance_uses_the_same_guard(self, db_session):
        from app.proposals.send_guard import placeholder_problem
        proposal = SimpleNamespace(content={"sections": _government_sections()}, total_value=100.0,
                                   used_fallback_template=False)
        assert placeholder_problem(proposal, db_session)["code"] == "unapproved_sections"

    def test_documented_and_studio_segment_text_counts_as_missing_until_approved(self, client):
        from app.proposals import document
        assert document._open_section("sections_library.government_scope") is True
        approve(client, "PRO-8")
        assert document._open_section("sections_library.government_scope") is False
        assert document._open_section("sections_library.residential_rooftop_scope") is True   # still a placeholder


class TestNoZeroPrice:
    def test_null_tiers_give_no_price_and_an_internal_note(self):
        cfg = content_engine.load_content_config()
        lead = SimpleNamespace(id=1, collected_fields={"project_type": "ground_mounted", "segment": "commercial",
                                                       "capacity_kw": 900, "location": "Patna"})
        content = content_engine.compose_content({"lead": lead, "customer": None}, cfg)
        pricing = next(s for s in content["sections"] if s["id"] == "pricing")
        assert content["pricing"]["total_value"] is None and content["pricing"]["rate_per_kw"] is None
        assert pricing["body"] == "[PLACEHOLDER: Price to be set by Grid Nova.]"
        assert "0" not in pricing["body"]

    def test_send_guard_refuses_without_a_price(self, db_session):
        for total in (None, 0, 0.0):
            proposal = SimpleNamespace(content={"sections": []}, total_value=total, used_fallback_template=False)
            problem = send_guard.placeholder_problem(proposal, db_session)
            assert problem["code"] == "placeholder_content" and problem["missing_total_value"] is True
            assert "Price to be set by Grid Nova" in problem["message"]

    def test_adjust_pricing_refuses_zero(self, client, memory, db_session):
        pid = _proposal(memory, db_session, [], total=None)
        r = client.patch(f"{DASH}/proposals/{pid}/adjust-pricing", json={"new_value": 0, "reason": "free?"},
                         headers=admin_headers())
        assert r.status_code == 422
        db_session.expire_all()
        assert memory.get_proposal(pid).total_value is None

    def test_no_price_never_reaches_acceptance_or_milestones(self, client, memory, db_session):
        pid = _proposal(memory, db_session, [], total=None)
        r = _approve_and_send(client, pid)
        assert r.status_code == 409 and r.json()["detail"]["missing_total_value"] is True
        db_session.expire_all()
        proposal = memory.get_proposal(pid)
        assert proposal.accept_token is None and proposal.accepted_at is None   # nothing to accept, no milestones

    def test_tier_registry_entry_is_resolved_by_design(self):
        entry = registry.load().by_id["PRO-13"]
        assert entry.status == "resolved" and "manual pricing by design" in entry.status_note


def test_approval_timestamps_are_utc_and_recent(client, db_session):
    approve(client, "PRO-10")
    row = db_session.execute(select(PlaceholderResolution)).scalar_one()
    at = row.resolved_at if row.resolved_at.tzinfo else row.resolved_at.replace(tzinfo=timezone.utc)
    assert (datetime.now(timezone.utc) - at).total_seconds() < 60
