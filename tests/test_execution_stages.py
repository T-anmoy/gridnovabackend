"""Phase 4A Step 2 — execution stages: start, applicability, two-step movement (associate
submits, Operations verifies/rejects), direct recording, evidence, payment gate, aging."""
import pytest
from sqlalchemy import select

from app.execution import config as execution_config
from app.memory.models import (
    AssociateCapability, AuditLog, CustomerOnboarding, OnboardingStatus, ProjectExecution, ProjectStage,
    ProjectStageEvent, StageSubmission, TimelineEvent,
)
from tests.execution_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ADMIN, ALIPURDUAR, ALIPURDUAR_I, CAMERA, DASH, ENG, KOLKATA, MGMT, OPS, QUEUE, SALES, advance_to,
    associate_in_execution, days_ago, exec_url, headers, jpeg, kyc_ready, make_associate, new_customer,
    notifications, onboarding_ready, staff_exec_url, territories, upload,
)

MONEY_KEYS = {"amount", "amount_due", "total_value", "price", "percent", "balance", "commission",
              "procurement_price_inr", "rate_per_kw"}


def _keys(node) -> set:
    if isinstance(node, dict):
        return set(node) | set().union(*(_keys(v) for v in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(_keys(v) for v in node)) if node else set()
    return set()


def _submit(client, gna, oid, stage, **text):
    return client.post(f"{exec_url(oid)}/submissions", json={"stage": stage, "text": text}, headers=headers(gna))


class TestStart:
    @pytest.mark.parametrize("capability, level, territory, mode", [
        (AssociateCapability.execution, None, None, "associate"),
        (AssociateCapability.sales, None, None, "grid_nova"),
        (AssociateCapability.sales, "district_head", ALIPURDUAR, "associate"),
    ])
    def test_mode_is_fixed_at_agreement(self, client, db_session, onboarding_ready, capability, level, territory,
                                        mode):
        gna, oid = associate_in_execution(client, db_session, capability=capability, level=level, territory=territory)
        record = db_session.execute(select(ProjectExecution)).scalar_one()
        assert record.execution_mode.value == mode and record.onboarding_id == oid
        audit = db_session.execute(select(AuditLog).where(AuditLog.entity_type == "project_execution")).scalar_one()
        assert audit.action == "started" and audit.after_value["execution_mode"] == mode
        assert client.get(exec_url(oid), headers=headers(gna)).json()["execution_mode"] == mode

    def test_nothing_before_the_signed_agreement(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session, capability=AssociateCapability.execution)
        oid = new_customer(client, gna)
        advance_to(db_session, oid, ProjectStage.accepted)
        r = client.get(exec_url(oid), headers=headers(gna))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "execution_not_started"
        assert db_session.execute(select(ProjectExecution)).first() is None
        # Operations cannot see a project before agreement_signed either.
        assert client.get(staff_exec_url(oid), headers=OPS).status_code == 403

class TestApplicability:
    def _stages(self, client, gna, oid):
        return [s["stage"] for s in client.get(exec_url(oid), headers=headers(gna)).json()["stages"]]

    def test_on_grid_not_pm_surya_ghar(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        assert self._stages(client, gna, oid) == ["feasibility_submitted", "material_dispatched", "installed",
                                                  "inspection_net_meter", "commissioned", "handover_complete"]

    def test_off_grid_skips_the_discom_stages(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session, connection_type="off_grid")
        assert self._stages(client, gna, oid) == ["material_dispatched", "installed", "commissioned",
                                                  "handover_complete"]

    def test_pm_surya_ghar_gets_every_stage(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session, capability=AssociateCapability.execution)
        oid = new_customer(client, gna, scheme_linked=True, scheme_name="pm_surya_ghar")
        advance_to(db_session, oid, ProjectStage.portal_agreement_approved)
        assert self._stages(client, gna, oid) == [s.value for s in execution_config.EXECUTION_STAGES]

    def test_progress_shows_execution_and_what_does_not_apply(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        t = client.get(f"/api/v1/associate/onboardings/{oid}/progress", headers=headers(gna)).json()
        assert t["next_stage"] == "feasibility_submitted" and t["execution_mode"] == "associate"
        assert {s["stage"] for s in t["not_applicable"]} == {"portal_agreement_approved", "subsidy_claimed",
                                                              "subsidy_received"}
        assert t["aging"]["stuck_threshold_days"] == 15 and t["aging"]["is_stuck"] is False
        staff = client.get(f"{DASH}/onboardings/{oid}/progress", headers=ADMIN).json()
        assert staff["execution_mode"] == "associate"


class TestTwoStep:
    def test_submit_verify(self, client, db_session, onboarding_ready, notifications):
        gna, oid = associate_in_execution(client, db_session)
        missing = _submit(client, gna, oid, "feasibility_submitted")
        assert missing.status_code == 422 and missing.json()["detail"]["missing"] == ["text:feasibility_reference"]
        r = _submit(client, gna, oid, "feasibility_submitted", feasibility_reference="WBSEDCL/APP/991")
        assert r.status_code == 201, r.text
        sub = r.json()
        assert sub["status"] == "pending" and sub["evidence"]["text"] == {"feasibility_reference": "WBSEDCL/APP/991"}
        assert _submit(client, gna, oid, "feasibility_submitted",
                       feasibility_reference="x").json()["detail"]["code"] == "submission_pending"
        assert _submit(client, gna, oid, "material_dispatched",
                       dispatch_reference="x").json()["detail"]["code"] == "stage_out_of_order"
        assert [p["stage"] for p in notifications.of("stage_submitted")] == ["feasibility_submitted"]
        assert notifications.of("stage_submitted")[0]["onboarding_id"] == oid

        detail = client.get(staff_exec_url(oid), headers=OPS).json()
        assert detail["pending_submission"]["submission_id"] == sub["submission_id"]
        r = client.post(f"{DASH}/execution/submissions/{sub['submission_id']}/verify", json={}, headers=OPS)
        assert r.status_code == 200, r.text
        event = db_session.execute(select(ProjectStageEvent).where(
            ProjectStageEvent.stage == ProjectStage.feasibility_submitted)).scalar_one()
        assert event.recorded_by == "emp-ops-1" and event.evidence["via"] == "associate_submission"
        assert event.evidence["submitted_by"] == f"associate:{gna}"
        assert event.evidence["text"]["feasibility_reference"] == "WBSEDCL/APP/991"
        assert r.json()["current_stage"] == "feasibility_submitted" and r.json()["pending_submission"] is None
        again = client.post(f"{DASH}/execution/submissions/{sub['submission_id']}/verify", json={}, headers=OPS)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "submission_not_pending"
        actions = {a.action for a in db_session.execute(select(AuditLog).where(
            AuditLog.entity_type == "stage_submission")).scalars()}
        assert actions == {"submitted", "verified"}

    def test_reject_then_resubmit(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        first = _submit(client, gna, oid, "feasibility_submitted", feasibility_reference="WRONG").json()
        url = f"{DASH}/execution/submissions/{first['submission_id']}/reject"
        assert client.post(url, json={"note": " "}, headers=OPS).json()["detail"]["code"] == "note_required"
        r = client.post(url, json={"note": "Reference does not match the DISCOM portal."}, headers=OPS)
        assert r.status_code == 200
        stage = next(s for s in r.json()["stages"] if s["stage"] == "feasibility_submitted")
        assert stage["submission"]["status"] == "rejected"
        assert stage["submission"]["review_note"] == "Reference does not match the DISCOM portal."
        mine = client.get(exec_url(oid), headers=headers(gna)).json()
        assert next(s for s in mine["stages"] if s["stage"] == "feasibility_submitted")["submission"]["status"] \
            == "rejected"
        second = _submit(client, gna, oid, "feasibility_submitted", feasibility_reference="WBSEDCL/APP/992")
        assert second.status_code == 201 and second.json()["submission_id"] != first["submission_id"]
        assert len(db_session.execute(select(StageSubmission)).scalars().all()) == 2
        timeline = [t.event_type.value for t in db_session.execute(select(TimelineEvent)).scalars()]
        assert "execution_stage_submitted" in timeline and "execution_stage_rejected" in timeline

    @pytest.mark.parametrize("who, code", [(SALES, 403), (MGMT, 403), (ENG, 403), (ACCOUNTS, 403)])
    def test_only_operations_or_admin_verify(self, client, db_session, onboarding_ready, who, code):
        gna, oid = associate_in_execution(client, db_session)
        sid = _submit(client, gna, oid, "feasibility_submitted", feasibility_reference="R1").json()["submission_id"]
        assert client.post(f"{DASH}/execution/submissions/{sid}/verify", json={}, headers=who).status_code == code
        assert client.post(f"{DASH}/execution/submissions/{sid}/verify", json={}, headers=ADMIN).status_code == 200

    def test_grid_nova_mode_is_recorded_directly(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session, capability=AssociateCapability.sales)
        assert _submit(client, gna, oid, "feasibility_submitted",
                       feasibility_reference="x").json()["detail"]["code"] == "execution_mode_grid_nova"
        assert upload(client, headers(gna), exec_url(oid), document_type="dispatch_challan").json()["detail"][
            "code"] == "execution_mode_grid_nova"
        url = f"{staff_exec_url(oid)}/feasibility_submitted"
        assert client.post(url, json={"text": {"feasibility_reference": "R"}}, headers=SALES).status_code == 403
        r = client.post(url, json={"text": {"feasibility_reference": "R-77"}, "note": "Filed today."}, headers=OPS)
        assert r.status_code == 200, r.text
        event = db_session.execute(select(ProjectStageEvent).where(
            ProjectStageEvent.stage == ProjectStage.feasibility_submitted)).scalar_one()
        assert event.evidence["via"] == "direct" and event.note == "Filed today."
        assert upload(client, OPS, staff_exec_url(oid), document_type="dispatch_challan").status_code == 201
        r = client.post(f"{staff_exec_url(oid)}/material_dispatched", json={"text": {"dispatch_reference": "D-1"}},
                        headers=ADMIN)
        assert r.status_code == 200
        ev = db_session.execute(select(ProjectStageEvent).where(
            ProjectStageEvent.stage == ProjectStage.material_dispatched)).scalar_one().evidence
        assert "dispatch_challan" in ev["documents"]

    def test_direct_recording_refused_in_associate_mode(self, client, db_session, onboarding_ready):
        _, oid = associate_in_execution(client, db_session)
        r = client.post(f"{staff_exec_url(oid)}/feasibility_submitted",
                        json={"text": {"feasibility_reference": "R"}}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "execution_mode_associate"

    def test_unknown_and_non_execution_stages(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        assert _submit(client, gna, oid, "nonsense").json()["detail"]["code"] == "not_an_execution_stage"
        assert _submit(client, gna, oid, "verified").json()["detail"]["code"] == "not_an_execution_stage"
        # Execution stages are not hand-recordable through the sales progress endpoint.
        r = client.post(f"{DASH}/onboardings/{oid}/progress/feasibility_submitted", json={"note": "x"}, headers=ADMIN)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "stage_recorded_by_its_action"

    def test_installed_needs_photos_documents_and_a_verified_register(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        advance_to(db_session, oid, ProjectStage.material_dispatched)
        missing = _submit(client, gna, oid, "installed").json()["detail"]["missing"]
        assert missing == ["document:oem_dcr_declaration", "photo:array_installed", "photo:inverter_installed",
                           "photo:earthing", "photo:meter_and_net_meter", "photo:completion_overview",
                           "register:verified"]

    def test_closed_customer_is_read_only(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        db_session.get(CustomerOnboarding, oid).status = OnboardingStatus.closed
        db_session.commit()
        assert _submit(client, gna, oid, "feasibility_submitted",
                       feasibility_reference="x").json()["detail"]["code"] == "not_editable"
        assert upload(client, headers(gna), exec_url(oid),
                      document_type="dispatch_challan").json()["detail"]["code"] == "not_editable"
        assert client.get(exec_url(oid), headers=headers(gna)).status_code == 200


class TestPaymentGate:
    def test_off_by_default(self, client, db_session, onboarding_ready):
        assert execution_config.execution()["gates"]["material_dispatched"]["enabled"] is False

    def test_when_enabled_dispatch_waits_and_says_nothing_about_money(self, client, db_session, onboarding_ready,
                                                                      monkeypatch):
        gate = execution_config.execution()["gates"]["material_dispatched"]
        monkeypatch.setitem(gate, "enabled", True)
        gna, oid = associate_in_execution(client, db_session)
        advance_to(db_session, oid, ProjectStage.feasibility_submitted)
        sid = _submit(client, gna, oid, "material_dispatched", dispatch_reference="D-9").json()["submission_id"]
        r = client.post(f"{DASH}/execution/submissions/{sid}/verify", json={}, headers=OPS)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "payment_gate"
        assert not _keys(r.json()) & MONEY_KEYS
        detail = client.get(staff_exec_url(oid), headers=OPS).json()
        blockers = next(s for s in detail["stages"] if s["stage"] == "material_dispatched")["blockers"]
        assert [b["code"] for b in blockers] == ["payment_gate"]


class TestAgingAndQueue:
    def test_stuck_after_the_threshold(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        days_ago(db_session, oid, ProjectStage.agreement_signed, 16)
        body = client.get(exec_url(oid), headers=headers(gna)).json()
        assert body["aging"] == {"days_in_stage": 16, "stuck_threshold_days": 15, "is_stuck": True}
        queue = client.get(QUEUE, params={"needs_attention": True}, headers=OPS).json()
        assert [i["onboarding_id"] for i in queue["items"]] == [oid]
        assert queue["items"][0]["attention_reasons"] == ["stuck"]
        assert client.get(QUEUE, params={"stuck": False}, headers=OPS).json()["total"] == 0

    def test_stuck_alert_emails_once_per_stall(self, client, db_session, onboarding_ready, notifications):
        _, oid = associate_in_execution(client, db_session)
        days_ago(db_session, oid, ProjectStage.agreement_signed, 20)
        url = f"{DASH}/internal/check-execution-alerts"
        assert client.post(url, headers=ADMIN).json() == {"stuck": 1, "newly_alerted": 1}
        assert client.post(url, headers=ADMIN).json() == {"stuck": 1, "newly_alerted": 0}
        [payload] = notifications.of("execution_stuck")
        assert payload["projects"][0]["onboarding_id"] == oid
        assert payload["projects"][0]["next_stage"] == "feasibility_submitted"
        assert client.post(url, headers=OPS).status_code == 403

    def test_pending_submission_needs_attention(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        _submit(client, gna, oid, "feasibility_submitted", feasibility_reference="R")
        item = client.get(QUEUE, headers=OPS).json()["items"][0]
        assert item["attention_reasons"] == ["pending_verification"]
        assert item["pending_submission"]["stage"] == "feasibility_submitted"

    def test_queue_scoping_and_no_money(self, client, db_session, onboarding_ready):
        _, oid = associate_in_execution(client, db_session)
        for who in (OPS, ADMIN, MGMT, ENG):
            body = client.get(QUEUE, headers=who).json()
            assert body["total"] == 1 and not _keys(body) & MONEY_KEYS
        assert client.get(QUEUE, headers=ACCOUNTS).status_code == 403
        # Sales: own or unassigned — this lead is unassigned.
        assert client.get(QUEUE, headers=SALES).json()["total"] == 1
        from app.memory.models import Lead
        from tests.conftest import seed_employees
        seed_employees(db_session, "emp-sales-9")
        db_session.get(Lead, db_session.get(CustomerOnboarding, oid).lead_id).assigned_employee_id = "emp-sales-9"
        db_session.commit()
        assert client.get(QUEUE, headers=SALES).json()["total"] == 0
        assert client.get(staff_exec_url(oid), headers=SALES).status_code == 403
        detail = client.get(staff_exec_url(oid), headers=OPS).json()
        assert detail["customer"]["mobile"] and not _keys(detail) & MONEY_KEYS
        assert "mobile" not in client.get(staff_exec_url(oid), headers=ENG).json()["customer"]
        assert client.get(QUEUE, params={"stage": "bogus"}, headers=OPS).status_code == 422


class TestAssociateScope:
    def test_territory_preferred_reads_but_does_not_act(self, client, db_session, onboarding_ready):
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        sibling = make_associate(db_session, level="district_head", territory=KOLKATA)
        gna, oid = associate_in_execution(client, db_session)   # site in Alipurduar
        assert client.get(exec_url(oid), headers=headers(head)).json()["can_submit"] is False
        assert client.get(exec_url(oid), headers=headers(sibling)).status_code == 403
        assert _submit(client, head, oid, "feasibility_submitted", feasibility_reference="x").status_code == 403
        assert upload(client, headers(head), exec_url(oid), document_type="dispatch_challan").status_code == 403
        assert client.get(exec_url(oid), headers=headers(gna)).json()["can_submit"] is True

    def test_staff_and_associate_tokens_do_not_cross(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        assert client.get(exec_url(oid), headers=ADMIN).status_code == 401
        assert client.get(staff_exec_url(oid), headers=headers(gna)).status_code == 401


class TestUploads:
    def test_evidence_upload_and_link(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        r = upload(client, headers(gna), exec_url(oid), photo_slot="earthing", **CAMERA)
        assert r.status_code == 201, r.text
        photo = r.json()["files"]["photos"]["earthing"]
        assert photo["capture_method"] == "camera" and photo["location_verified"] is True and photo["flags"] == []
        link = client.get(f"{exec_url(oid)}/files/{photo['file_ref']}", headers=headers(gna))
        assert link.status_code == 200 and link.json()["filename"] == "file.jpg"
        assert client.get(f"{staff_exec_url(oid)}/files/{photo['file_ref']}", headers=OPS).status_code == 200
        assert upload(client, headers(gna), exec_url(oid), photo_slot="nope").json()["detail"]["code"] == \
            "unknown_photo_slot"
        assert upload(client, headers(gna), exec_url(oid), document_type="nope").json()["detail"]["code"] == \
            "unknown_document_type"

    def test_oversized_upload_refused_before_the_route(self, client, db_session, onboarding_ready, monkeypatch):
        from app.execution import files
        monkeypatch.setattr(files, "MAX_FILE_BYTES", 1024)
        gna, oid = associate_in_execution(client, db_session)
        big = b"%PDF-1.4\n" + b"x" * (200 * 1024)
        r = upload(client, headers(gna), exec_url(oid), document_type="dispatch_challan", content=big)
        assert r.status_code == 413
        r = upload(client, OPS, staff_exec_url(oid), document_type="dispatch_challan", content=big)
        assert r.status_code == 413


class TestEmails:
    def _notifier(self, monkeypatch, role_emails=None):
        import httpx
        from app.notifications.notifier import EmailNotifier, LogNotifier
        sent = []

        class _Resp:
            status_code = 200
        monkeypatch.setattr(httpx, "post", lambda url, headers, json, timeout: sent.append(json) or _Resp())
        return EmailNotifier("key", "Grid Nova <n@x.test>", ["staff@x.test"], "https://app.test", LogNotifier(),
                             role_emails=role_emails), sent

    def test_stage_submitted_goes_to_operations_and_links_the_customer(self, monkeypatch):
        n, sent = self._notifier(monkeypatch, {"operations": ["ops@x.test"], "accounts": []})
        n.notify("stage_submitted", {"submission_id": 3, "onboarding_id": 12, "stage": "installed",
                                     "stage_label": "Installed", "submitted_by": "associate:GNA-WB-26-00001",
                                     "customer_name": "Asha Roy"})
        [mail] = sent
        assert mail["to"] == ["ops@x.test"] and mail["subject"] == "Stage to check: Installed — Asha Roy"
        assert "https://app.test/dashboard/onboardings/12" in mail["text"]

    def test_falls_back_to_staff_when_operations_has_no_address(self, monkeypatch):
        n, sent = self._notifier(monkeypatch, {"operations": []})
        n.notify("execution_stuck", {"projects": [{"onboarding_id": 5, "customer_name": "Ravi", "next_stage_label":
                                                   "Installed", "days_in_stage": 12, "stuck_threshold_days": 10}]})
        [mail] = sent
        assert mail["to"] == ["staff@x.test"] and mail["subject"] == "1 project stuck in execution"
        assert "https://app.test/dashboard/onboardings/5" in mail["text"]
        assert "waiting for Installed · 12 days (expected within 10)" in mail["text"]
