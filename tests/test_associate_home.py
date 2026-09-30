"""Phase 4A Step 7B — the associate's to-do feed, progress_summary on list items (without
an N+1), and the three associate emails."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select

from app.memory.models import (
    AssociateCapability, CustomerAgreement, CustomerOnboarding, Proposal, ProposalStatus, ProjectStage,
)
from tests.execution_support import (  # noqa: F401 — fixtures
    ADMIN, ALIPURDUAR, BASE, DASH, OPS, SALES, advance_to, associate_in_execution, days_ago, exec_url, headers,
    kyc_ready, make_associate, new_customer, notifications, onboarding_ready, territories,
)
from tests.onboarding_support import upload_required_documents
from tests.payments_support import ACCOUNTS, PAY, accepted_customer, neft, record, stub_pdf  # noqa: F401

HOME = "/api/v1/associate/home"


def _feed(client, gna):
    body = client.get(HOME, headers=headers(gna)).json()
    return body, {(i["onboarding_id"], i["kind"]): i for i in body["items"]}


class TestFeed:
    def test_draft_documents_then_ready_then_waiting(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        body, items = _feed(client, gna)
        item = items[(oid, "documents_missing")]
        assert item["message_key"] == "todo.documents_missing" and item["priority"] == 1
        assert item["params"]["missing_documents"] == ["electricity_bill", "id_proof", "ownership_proof", "photo"]
        assert body["totals"] == {"customers": 1, "awaiting_grid_nova": 0, "needing_action": 1}
        upload_required_documents(client, gna, oid)
        _, items = _feed(client, gna)
        assert set(items) == {(oid, "ready_to_submit")}
        client.post(f"{BASE}/{oid}/submit", headers=headers(gna))
        body, items = _feed(client, gna)
        assert items == {} and body["totals"] == {"customers": 1, "awaiting_grid_nova": 1, "needing_action": 0}

    def test_proposal_and_agreement(self, client, db_session, onboarding_ready):
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        advance_to(db_session, oid, ProjectStage.proposal_prepared)
        _, items = _feed(client, gna)
        assert (oid, "formal_proposal_ready") in items
        onboarding = db_session.get(CustomerOnboarding, oid)
        proposal = Proposal(lead_id=onboarding.lead_id, version=1, status=ProposalStatus.accepted, content={},
                            total_value=100000.0)
        db_session.add(proposal)
        db_session.commit()
        advance_to(db_session, oid, ProjectStage.accepted)
        db_session.add(CustomerAgreement(onboarding_id=oid, proposal_id=proposal.id, agreement_storage_key="k",
                                         generated_at=datetime.now(timezone.utc), generated_by="emp-sales-1"))
        db_session.commit()
        _, items = _feed(client, gna)
        assert (oid, "formal_proposal_ready") not in items and (oid, "upload_signed_agreement") in items
        assert items[(oid, "payment_due")]["params"] == {"milestone_number": 1, "label": "On acceptance",
                                                         "outstanding": "20000.00", "overdue": False}

    def test_execution_rejected_and_next_step(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        _, items = _feed(client, gna)
        assert items[(oid, "execution_next_step")]["params"]["stage"] == "feasibility_submitted"
        sid = client.post(f"{exec_url(oid)}/submissions", json={"stage": "feasibility_submitted",
                                                                "text": {"feasibility_reference": "R"}},
                          headers=headers(gna)).json()["submission_id"]
        _, items = _feed(client, gna)
        assert (oid, "execution_next_step") not in items           # waiting on Grid Nova now
        client.post(f"{DASH}/execution/submissions/{sid}/reject", json={"note": "Wrong reference."}, headers=OPS)
        _, items = _feed(client, gna)
        rejected = items[(oid, "stage_rejected")]
        assert rejected["priority"] == 1 and rejected["params"]["review_note"] == "Wrong reference."
        assert (oid, "execution_next_step") not in items

    def test_payment_overdue_and_receipt(self, client, db_session, onboarding_ready, stub_pdf):
        gna, oid = accepted_customer(client, db_session)
        days_ago(db_session, oid, ProjectStage.accepted, 10)
        _, items = _feed(client, gna)
        due = items[(oid, "payment_due")]
        assert due["message_key"] == "todo.payment_overdue" and due["priority"] == 1
        from app.execution import config
        receipt_cfg = config.payments()["receipt"]
        pid = record(client, ACCOUNTS, oid, neft(), staff=True).json()["payment_id"]
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(receipt_cfg, "company", {**receipt_cfg["company"], "gstin": "G", "bank_name": "B",
                                                "account_number": "1", "ifsc": "I"})
            mp.setitem(receipt_cfg, "wording", {"heading": "Receipt", "footer": "Not a tax invoice."})
            client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)
        _, items = _feed(client, gna)
        assert (oid, "payment_due") not in items
        assert items[(oid, "receipt_available")]["priority"] == 3

    def test_message_keys_only_and_own_customers_only(self, client, db_session, onboarding_ready):
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        downline = make_associate(db_session)
        theirs = new_customer(client, downline)                   # sited in the head's territory
        mine = new_customer(client, head)
        body = client.get(HOME, headers=headers(head)).json()
        assert {i["onboarding_id"] for i in body["items"]} == {mine} and theirs != mine
        for item in body["items"]:
            assert set(item) == {"kind", "message_key", "onboarding_id", "customer_name", "priority", "params"}
            assert item["message_key"].startswith("todo.") and " " not in item["message_key"]


class TestProgressSummary:
    def test_on_both_lists(self, client, db_session, onboarding_ready):
        gna, oid = associate_in_execution(client, db_session)
        [item] = client.get(BASE, headers=headers(gna)).json()["items"]
        assert item["progress_summary"] == {
            "current_stage": "agreement_signed", "stage_label": "Agreement signed",
            "next_stage": "feasibility_submitted", "next_stage_label": "DISCOM feasibility submitted",
            # The test shortcut leaves the record a draft, so its documents are still due too.
            "needs_action": True, "action_kinds": ["documents_missing", "execution_next_step"], "blocked": None}
        staff_item = client.get(f"{DASH}/onboardings", headers=ADMIN).json()["items"][0]
        assert staff_item["progress_summary"]["next_stage"] == "feasibility_submitted"

    def test_blocked_marker(self, client, db_session, onboarding_ready, monkeypatch):
        from app.execution import config
        monkeypatch.setitem(config.execution()["gates"]["material_dispatched"], "enabled", True)
        gna, oid = associate_in_execution(client, db_session)
        advance_to(db_session, oid, ProjectStage.feasibility_submitted)
        [item] = client.get(BASE, headers=headers(gna)).json()["items"]
        assert item["progress_summary"]["blocked"] == {"reason": "payment_gate"}

    @pytest.mark.parametrize("staff", [False, True])
    def test_no_n_plus_one(self, client, db_session, onboarding_ready, staff):
        gna = make_associate(db_session)
        url, who = (f"{DASH}/onboardings", ADMIN) if staff else (BASE, headers(gna))
        engine = db_session.get_bind()
        counts = []
        for n in (1, 6):
            while len(client.get(url, headers=who).json()["items"]) < n:
                oid = new_customer(client, gna)
                advance_to(db_session, oid, ProjectStage.verified)
            statements = []

            def count(*args, **kwargs):
                statements.append(1)
            event.listen(engine, "before_cursor_execute", count)
            client.get(url, headers=who)
            event.remove(engine, "before_cursor_execute", count)
            counts.append(len(statements))
        assert counts[0] == counts[1], f"queries grew with the page: {counts}"


class TestAssociateEmails:
    def test_proposal_approved(self, client, db_session, onboarding_ready, notifications):
        from app.memory.memory_manager import MemoryManager
        gna = make_associate(db_session)
        oid = new_customer(client, gna)
        onboarding = db_session.get(CustomerOnboarding, oid)
        proposal = Proposal(lead_id=onboarding.lead_id, version=1, status=ProposalStatus.draft, content={})
        db_session.add(proposal)
        db_session.commit()
        MemoryManager(db_session).transition_proposal_status(proposal.id, ProposalStatus.approved, "emp-admin-1",
                                                           approved_by_employee_id="emp-admin-1")
        db_session.commit()
        [mail] = notifications.of("associate_email")
        assert mail["event_type"] == "associate_proposal_ready" and mail["to"].startswith("assoc")
        assert mail["subject"] == "Proposal ready for Customer " + mail["subject"].split("Customer ")[1]

    def test_stage_rejected(self, client, db_session, onboarding_ready, notifications):
        gna, oid = associate_in_execution(client, db_session)
        sid = client.post(f"{exec_url(oid)}/submissions", json={"stage": "feasibility_submitted",
                                                                "text": {"feasibility_reference": "R"}},
                          headers=headers(gna)).json()["submission_id"]
        client.post(f"{DASH}/execution/submissions/{sid}/reject", json={"note": "Wrong reference."}, headers=OPS)
        [mail] = notifications.of("associate_email")
        assert mail["event_type"] == "associate_stage_rejected" and "Wrong reference." in " ".join(mail["lines"])

    def test_payment_verified_link_only_when_not_held(self, client, db_session, onboarding_ready, stub_pdf,
                                                      notifications):
        gna, oid = accepted_customer(client, db_session)
        pid = record(client, ACCOUNTS, oid, neft(), staff=True).json()["payment_id"]
        client.post(f"{PAY}/{pid}/verify", headers=ACCOUNTS)   # shipped config: DUMMY → held
        [mail] = notifications.of("associate_email")
        assert mail["event_type"] == "associate_payment_verified" and mail["link_url"] is None
        assert "20000.00" in mail["lines"][0]

    def test_switched_off_or_no_email(self, client, db_session, onboarding_ready, notifications, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "notify_associates", False)
        gna, oid = associate_in_execution(client, db_session)
        sid = client.post(f"{exec_url(oid)}/submissions", json={"stage": "feasibility_submitted",
                                                                "text": {"feasibility_reference": "R"}},
                          headers=headers(gna)).json()["submission_id"]
        client.post(f"{DASH}/execution/submissions/{sid}/reject", json={"note": "x"}, headers=OPS)
        assert notifications.of("associate_email") == []
