"""Phase 4A Step 3 — the permission matrix: every staff role × every Phase 2–4 endpoint,
and every associate relationship × every associate endpoint.

The matrix is route-complete: TestMatrixIsComplete fails when a route in one of the
covered routers has no row here, so a new endpoint cannot ship without a decision on
who may call it. For each staff route, a role outside `allowed` must get 403 and a
role inside must not (it may still get a business 4xx — the gate is what is tested).
Operations' successful responses must carry no monetary field. Associate tokens are
refused on staff routes and staff tokens on associate routes.
"""
import importlib

import pytest

from app.memory.models import AssociateCapability, ProjectStage
from tests.execution_support import (  # noqa: F401 — fixtures
    ALIPURDUAR, KOLKATA, PDF, advance_to, headers, kyc_ready, make_associate, new_customer, onboarding_ready,
    territories,
)

ROLES = ("admin", "management", "sales", "engineer", "operations", "accounts")
ADMIN_ONLY = {"admin"}
ONBOARDING_READERS = {"admin", "management", "sales"}
ONBOARDING_ACTORS = {"admin", "sales"}
EXECUTION_READERS = {"admin", "management", "sales", "engineer", "operations"}
EXECUTION_ACTORS = {"admin", "operations"}
SERVICE_ACTORS = {"admin", "operations", "engineer"}
SR_FORM = {"data": {"category": "no_generation", "description": "No power"}}
MONEY_KEYS = {"amount", "amount_due", "total_value", "price", "percent", "balance", "commission",
              "procurement_price_inr", "rate_per_kw", "final_price", "subtotal", "gst"}

COVERED_ROUTERS = ("associates_admin", "associate_portal", "associate_onboarding", "onboarding_admin",
                   "placeholders_admin", "associate_execution", "execution_admin", "execution_register_admin",
                   "associate_payments", "payments_admin", "pricing", "associate_equipment", "associate_survey",
                   "survey_admin", "commissions_admin", "associate_commissions", "network_admin", "handover_admin",
                   "associate_handover", "archive_admin", "nova_records", "associate_referrals")
ARCHIVE_READERS = set(ROLES)
SURVEY_READERS = {"admin", "management", "sales", "engineer"}
SURVEY_REVIEWERS = {"admin", "engineer"}
PNG_FILE = {"files": {"file": ("s.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")}}
PAY_READERS = {"admin", "management", "accounts", "sales"}
PAY_RECORDERS = {"admin", "accounts", "sales"}
PAY_PROCESSORS = {"admin", "accounts"}
COMM_READERS = {"admin", "management", "accounts"}
COMM_PROCESSORS = {"admin", "accounts"}
NEFT = {"amount": "100.00", "mode": "neft", "reference": "UTR-MATRIX-{role}"}
UNIT = {"category": "module", "equipment_id": "{equipment_id}", "serial_number": "MATRIX-NEW", "rating_wp": "540",
        "is_dcr": True}


def staff(role: str) -> dict:
    return {"X-Employee-Id": f"emp-{role}-1", "X-Employee-Role": role}


def upload_form(**fields):
    return {"data": fields, "files": {"file": ("f.pdf", PDF, "application/pdf")}}


# (method, path) → (allowed staff roles, request kwargs). Paths use {placeholders}
# filled from the scenario.
STAFF = {
    ("GET", "/api/v1/dashboard/associate-applications"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/associate-applications/{application_type}/{application_id}"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/associate-applications/{application_type}/{application_id}/documents/{document}"):
        (ADMIN_ONLY, {}),
    ("POST", "/api/v1/dashboard/associate-applications/{application_type}/{application_id}/status"):
        (ADMIN_ONLY, {"json": {"status": "needs_info", "note": "x"}}),
    ("POST", "/api/v1/dashboard/associate-applications/{application_type}/{application_id}/approve"):
        (ADMIN_ONLY, {"json": {"verification_call_completed": True, "verification_call_note": "x"}}),
    ("GET", "/api/v1/dashboard/associates"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/associates/{associate_id}"): (ADMIN_ONLY, {}),
    ("POST", "/api/v1/dashboard/associates/{associate_id}/status"):
        (ADMIN_ONLY, {"json": {"status": "suspended", "reason": "x"}}),
    ("POST", "/api/v1/dashboard/associates/{associate_id}/clerk-invite"): (ADMIN_ONLY, {}),
    ("POST", "/api/v1/dashboard/associates/{associate_id}/reassign-customers"):
        (ADMIN_ONLY, {"json": {"to_associate_id": "GNA-WB-26-99999", "reason": "x"}}),
    ("POST", "/api/v1/dashboard/associates/{associate_id}/documents/regenerate"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/associates/{associate_id}/documents/{document}"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/territories/{level}"): (ADMIN_ONLY, {}),
    ("POST", "/api/v1/dashboard/territories/districts"):
        (ADMIN_ONLY, {"json": {"state_lgd_code": 19, "lgd_code": 99991, "name": "Test District"}}),
    ("POST", "/api/v1/dashboard/territories/blocks"):
        (ADMIN_ONLY, {"json": {"district_lgd_code": ALIPURDUAR, "lgd_code": 99992, "name": "Test Block"}}),
    ("PATCH", "/api/v1/dashboard/territories/{level}/{lgd_code}"): (ADMIN_ONLY, {"json": {"name": "Alipurduar"}}),
    ("GET", "/api/v1/dashboard/territory-seats"): (ADMIN_ONLY, {}),
    ("POST", "/api/v1/dashboard/territory-seats/{seat_id}/release"): (ADMIN_ONLY, {"json": {"reason": "x"}}),
    ("GET", "/api/v1/dashboard/onboardings"): (ONBOARDING_READERS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}"): (ONBOARDING_READERS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/verification-call"):
        (ONBOARDING_ACTORS, {"json": {"verification_call_completed": True, "note": "x"}}),
    ("GET", "/api/v1/dashboard/attribution-flags"): (ADMIN_ONLY, {}),
    ("POST", "/api/v1/dashboard/attribution-flags/{flag_id}/resolve"):
        (ADMIN_ONLY, {"json": {"resolution": "keep_existing", "note": "x"}}),
    ("POST", "/api/v1/dashboard/internal/check-onboarding-escalations"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/progress"): (ONBOARDING_READERS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/progress/{stage}"): (ONBOARDING_ACTORS,
                                                                                {"json": {"note": "x"}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/agreement"): (ONBOARDING_ACTORS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/agreement"): (ONBOARDING_READERS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/agreement/signed"):
        (ONBOARDING_ACTORS, upload_form(stamp_paper_number="WB 1", stamp_paper_date="2026-09-01")),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/estimates"): (ONBOARDING_READERS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/formal-proposal"): (ONBOARDING_READERS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/formal-proposal"): (ONBOARDING_ACTORS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/proposal/in-person-acceptance"):
        (ONBOARDING_ACTORS, {"json": {"proposal_id": 1, "delivered_on": "2026-09-01", "note": "x",
                                      "reference": "x"}}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/survey"): (SURVEY_READERS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/survey/results"): (SURVEY_READERS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/survey/review"):
        (SURVEY_REVIEWERS, {"json": {"outcome": "feasible"}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/survey/report"): (SURVEY_REVIEWERS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/survey/report"): (SURVEY_READERS, {}),
    ("GET", "/api/v1/dashboard/survey-corrections"): (SURVEY_READERS, {}),
    ("GET", "/api/v1/dashboard/survey-reviews"): (SURVEY_READERS, {}),
    ("POST", "/api/v1/dashboard/internal/check-survey-reviews"): (ADMIN_ONLY, {}),
    ("POST", "/api/v1/dashboard/survey-corrections/{correction_id}/apply"): (SURVEY_REVIEWERS, {"json": {}}),
    ("POST", "/api/v1/dashboard/survey-corrections/{correction_id}/reject"):
        (SURVEY_REVIEWERS, {"json": {"note": "x"}}),
    ("POST", "/api/v1/dashboard/proposals/{proposal_id}/reverification/clear"):
        ({"admin", "management", "engineer"}, {"json": {"note": "x"}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/site-location"):
        (ONBOARDING_ACTORS, {"json": {"source": "pasted_coordinates", "text": "26.49, 89.52",
                                      "reason": "Confirmed on the call."}}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/handover"): (EXECUTION_READERS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/handover/orientation"):
        (EXECUTION_ACTORS, {"json": {"done": True}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/handover/om-contact"):
        (EXECUTION_ACTORS, {"json": {"done": True}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/handover/acknowledgement"):
        (EXECUTION_ACTORS, {"data": {"method": "in_person", "customer_name": "Asha"}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/handover/link"): (EXECUTION_ACTORS, {"json": {}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/handover/link/revoke"): (EXECUTION_ACTORS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/handover/pdf/regenerate"): (EXECUTION_ACTORS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/handover/pdf"): (EXECUTION_READERS, {}),
    ("GET", "/api/v1/dashboard/archive/template.csv"): (ADMIN_ONLY, {}),
    ("POST", "/api/v1/dashboard/archive/imports"):
        (ADMIN_ONLY, {"files": {"file": ("a.csv", b"scheme_or_segment,state,district,commissioning_date,capacity_kw\n",
                                          "text/csv")}}),
    ("GET", "/api/v1/dashboard/archive/imports"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/archive/imports/{batch_id}"): (ADMIN_ONLY, {}),
    ("POST", "/api/v1/dashboard/archive/imports/{batch_id}/void"): (ADMIN_ONLY, {"json": {"reason": "matrix test"}}),
    ("GET", "/api/v1/dashboard/archive/projects"): (ARCHIVE_READERS, {}),
    ("GET", "/api/v1/dashboard/archive/projects/{project_id}"): (ARCHIVE_READERS, {}),
    ("GET", "/api/v1/dashboard/archive/stats"): (ARCHIVE_READERS, {}),
    # BE-9 Nova Records: every staff role may ask (answers are scoped per role, tests/
    # test_records_tools.py); conversations and feedback are the caller's own (404 otherwise).
    ("POST", "/api/v1/dashboard/nova/ask"): (set(ROLES), {"json": {"question": "How many projects by district?"}}),
    ("GET", "/api/v1/dashboard/nova/conversations"): (set(ROLES), {}),
    ("GET", "/api/v1/dashboard/nova/conversations/{conversation_id}"): (set(ROLES), {}),
    ("POST", "/api/v1/dashboard/nova/turns/{turn_id}/feedback"): (set(ROLES), {"json": {"helpful": True}}),
    ("POST", "/api/v1/dashboard/nova/internal/check-nova-records-retention"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/service-requests"): (EXECUTION_READERS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/service-requests"): (EXECUTION_READERS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/service-requests"): (SERVICE_ACTORS, SR_FORM),
    ("GET", "/api/v1/dashboard/service-requests/{request_id}"): (EXECUTION_READERS, {}),
    ("POST", "/api/v1/dashboard/service-requests/{request_id}/assign"): (SERVICE_ACTORS, {"json": {"assignee_id": None}}),
    ("POST", "/api/v1/dashboard/service-requests/{request_id}/status"):
        (SERVICE_ACTORS, {"json": {"status": "in_progress"}}),
    ("POST", "/api/v1/dashboard/service-requests/{request_id}/notes"): (SERVICE_ACTORS, {"json": {"note": "x"}}),
    ("GET", "/api/v1/dashboard/service-requests/{request_id}/photos/{file_ref}"): (EXECUTION_READERS, {}),
    ("POST", "/api/v1/dashboard/internal/check-service-requests"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/placeholders"): ({"admin", "management"}, {}),
    ("POST", "/api/v1/dashboard/placeholders/{entry_id}/resolve"): (ADMIN_ONLY, {"json": {"note": "x"}}),
    ("GET", "/api/v1/dashboard/execution"): (EXECUTION_READERS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/execution"): (EXECUTION_READERS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/files/{file_ref}"): (EXECUTION_READERS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/files"):
        (EXECUTION_ACTORS, upload_form(kind="document", document_type="dispatch_challan")),
    ("POST", "/api/v1/dashboard/execution/submissions/{submission_id}/verify"): (EXECUTION_ACTORS, {"json": {}}),
    ("POST", "/api/v1/dashboard/execution/submissions/{submission_id}/reject"):
        (EXECUTION_ACTORS, {"json": {"note": "x"}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/{stage}"):
        (EXECUTION_ACTORS, {"json": {"text": {"feasibility_reference": "x"}}}),
    ("POST", "/api/v1/dashboard/internal/check-execution-alerts"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/units"): (EXECUTION_READERS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/units/check-serial"):
        (EXECUTION_READERS, {"params": {"serial": "MATRIX-1"}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/units"): (EXECUTION_ACTORS, {"json": UNIT}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/units/bulk"):
        (EXECUTION_ACTORS, {"json": {"units": [UNIT]}}),
    ("PATCH", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/units/{unit_id}"):
        (EXECUTION_ACTORS, {"json": {"rating_wp": "545"}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/units/{unit_id}/void"):
        (EXECUTION_ACTORS, {"json": {"reason": "x"}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/register/verify"): (EXECUTION_ACTORS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/dcr-override"):
        (ADMIN_ONLY, {"json": {"reason": "x"}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/mode"):
        (ADMIN_ONLY, {"json": {"execution_mode": "grid_nova", "reason": "x"}}),
    ("GET", "/api/v1/dashboard/payments"): (PAY_READERS, {}),
    ("GET", "/api/v1/dashboard/payment-milestones"): (PAY_READERS, {}),
    ("GET", "/api/v1/dashboard/payments/{payment_id}"): (PAY_READERS, {}),
    ("GET", "/api/v1/dashboard/payments/{payment_id}/proof"): (PAY_READERS, {}),
    ("POST", "/api/v1/dashboard/payments/{payment_id}/proof"): (PAY_RECORDERS, upload_form()),
    ("POST", "/api/v1/dashboard/payments/{payment_id}/deposit"): (PAY_PROCESSORS, {}),
    ("POST", "/api/v1/dashboard/payments/{payment_id}/verify"): (PAY_PROCESSORS, {}),
    ("POST", "/api/v1/dashboard/payments/{payment_id}/dishonour"): (PAY_PROCESSORS, {"json": {"reason": "x"}}),
    ("POST", "/api/v1/dashboard/payments/{payment_id}/cancel"): (PAY_PROCESSORS, {"json": {"reason": "x"}}),
    ("POST", "/api/v1/dashboard/payments/{payment_id}/receipt/resend"): (PAY_PROCESSORS, {}),
    ("POST", "/api/v1/dashboard/payment-receipts/release-held"): (PAY_PROCESSORS, {}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/payments"): (PAY_READERS, {}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/payments"): (PAY_RECORDERS, {"json": NEFT}),
    ("GET", "/api/v1/dashboard/onboardings/{onboarding_id}/payment-schedule"): (PAY_READERS, {}),
    ("PUT", "/api/v1/dashboard/onboardings/{onboarding_id}/payment-schedule"):
        (PAY_PROCESSORS, {"json": {"milestones": [{"label": "A", "percent": "100", "trigger": "accepted"}]}}),
    ("POST", "/api/v1/dashboard/internal/check-payment-alerts"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/exports/payments.csv"): (PAY_PROCESSORS, {}),
    ("GET", "/api/v1/dashboard/exports/receipts.csv"): (PAY_PROCESSORS, {}),
    ("GET", "/api/v1/dashboard/exports/ledger.csv"): (PAY_PROCESSORS, {}),
    # Every staff role may ask; each sees only the areas it has (tests/test_accounts_ops.py).
    ("GET", "/api/v1/dashboard/work-queues"): (set(ROLES), {}),
    # The unit in the scenario is already matched, so allowed roles get 409 already_matched —
    # the gate (403 for everyone else) is what is tested.
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/units/{unit_id}/match"):
        (EXECUTION_ACTORS, {"json": {"equipment_id": "{equipment_id}"}}),
    ("POST", "/api/v1/dashboard/onboardings/{onboarding_id}/execution/catalogue"):
        (EXECUTION_ACTORS, {"json": {"category": "structure", "brand": "Matrix Fab", "model_name": "GI-{role}",
                                     "is_dcr_compliant": False}}),
    # BE-4 commissions: Management reads, Accounts/Admin act, Admin sets rates; the bank
    # file (full account numbers) is Accounts only.
    ("GET", "/api/v1/dashboard/commissions"): (COMM_READERS, {}),
    ("POST", "/api/v1/dashboard/commissions/approve"): (COMM_PROCESSORS, {"json": {"entry_ids": [999999]}}),
    ("POST", "/api/v1/dashboard/commissions/{entry_id}/reverse"): (ADMIN_ONLY, {"json": {"reason": "x"}}),
    ("GET", "/api/v1/dashboard/commission-rate-schedules"): (COMM_READERS, {}),
    ("POST", "/api/v1/dashboard/commission-rate-schedules"):
        (ADMIN_ONLY, {"json": {"effective_from": "2026-01-01", "rates": {}}}),
    ("POST", "/api/v1/dashboard/commission-rate-schedules/{schedule_id}/re-rate"):
        (ADMIN_ONLY, {"json": {"reason": "x"}}),
    ("GET", "/api/v1/dashboard/payout-batches"): (COMM_READERS, {}),
    ("POST", "/api/v1/dashboard/payout-batches"): (COMM_PROCESSORS, {"json": {"period": "2000-01"}}),
    ("GET", "/api/v1/dashboard/payout-batches/{batch_id}"): (COMM_READERS, {}),
    ("POST", "/api/v1/dashboard/payout-batches/{batch_id}/approve"): (COMM_PROCESSORS, {"json": {}}),
    ("POST", "/api/v1/dashboard/payout-batches/{batch_id}/mark-paid"): (COMM_PROCESSORS, {"json": {"lines": []}}),
    ("POST", "/api/v1/dashboard/payout-batches/{batch_id}/cancel"): (COMM_PROCESSORS, {"json": {"reason": "x"}}),
    ("GET", "/api/v1/dashboard/exports/commissions.csv"): (COMM_PROCESSORS, {}),
    ("GET", "/api/v1/dashboard/exports/tds.csv"): (COMM_PROCESSORS, {}),
    ("GET", "/api/v1/dashboard/exports/payout-bank-{batch_id}.csv"): ({"accounts"}, {}),
    ("GET", "/api/v1/dashboard/commission-queries"): (COMM_READERS, {}),
    ("POST", "/api/v1/dashboard/commission-queries/{query_id}/respond"):
        (COMM_PROCESSORS, {"json": {"response": "x"}}),
    ("POST", "/api/v1/dashboard/internal/check-commission-queries"): (ADMIN_ONLY, {}),
    ("GET", "/api/v1/dashboard/network/summary"): ({"admin", "management"}, {}),
    # BE-12 individual rates: Admin sets, Management reads (with the internal reason) —
    # Accounts sees rates only as recorded on ledger entries.
    ("GET", "/api/v1/dashboard/associates/{associate_id}/commission-rate"): ({"admin", "management"}, {}),
    ("POST", "/api/v1/dashboard/associates/{associate_id}/commission-rate"):
        (ADMIN_ONLY, {"json": {"rate_percent": "101", "effective_from": "2026-01-01", "reason": "matrix gate check"}}),
    ("GET", "/api/v1/dashboard/associates/{associate_id}/performance"): ({"admin", "management"}, {}),
    # The placeholder engine's internal breakdown (PM-A: never customer-facing).
    ("POST", "/api/v1/pricing/breakdown"):
        ({"admin", "sales"}, {"json": {"segment": "residential", "size_kw": 5, "state": "West Bengal"}}),
}

# Public, unauthenticated routes (PM-A pricing): reachable with no credentials at all.
PUBLIC = {
    ("POST", "/api/v1/pricing/estimate"):
        {"json": {"segment": "residential", "size_kw": 5, "state": "West Bengal", "connection_phase": "single_phase"}},
    ("POST", "/api/v1/pricing/instant-estimate"):
        {"json": {"segment": "residential", "state": "West Bengal", "sizing_method": "by_units",
                  "monthly_units_kwh": 400, "connection_phase": "single_phase"}},
    ("GET", "/api/v1/pricing/packages"): {},
    ("GET", "/api/v1/pricing/estimate-assumptions"): {},
}

# Associate routes: "own" = not tied to one customer; "read"/"write" = one customer's.
ASSOCIATE = {
    ("GET", "/api/v1/associate/me"): ("own", {}),
    ("GET", "/api/v1/associate/me/documents/{document}"): ("own", {}),
    ("GET", "/api/v1/associate/territory/members"): ("own", {}),
    ("GET", "/api/v1/associate/onboardings/options"): ("own", {}),
    ("POST", "/api/v1/associate/onboardings"): ("own", {"json": {}}),
    ("GET", "/api/v1/associate/onboardings"): ("own", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}"): ("read", {}),
    ("PATCH", "/api/v1/associate/onboardings/{onboarding_id}"): ("write", {"json": {"floors": 1}}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/files"):
        ("write", upload_form(kind="document", document_type="noc")),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/files/{file_ref}"): ("documents", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/submit"): ("write", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/survey"): ("read", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/survey/results"): ("read", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/survey/signatures/{party}"): ("write", PNG_FILE),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/survey/corrections"):
        ("write", {"json": {"changes": {"floors": 3}, "reason": "x"}}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/survey/corrections"): ("read", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/survey/report"): ("documents", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/site-location"):
        ("write", {"json": {"source": "pasted_coordinates", "text": "26.49, 89.52"}}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/site-location/confirm"): ("write", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/progress"): ("read", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/request-formal-proposal"): ("write", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/estimates"): ("write", {"json": {}}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/estimates"): ("read", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/formal-proposal"): ("read", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/proposal/in-person-acceptance"):
        ("write", {"json": {"proposal_id": 1, "delivered_on": "2026-09-01", "note": "x", "reference": "x"}}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/agreement"): ("documents", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/agreement/signed"):
        ("write", upload_form(stamp_paper_number="WB 1", stamp_paper_date="2026-09-01")),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/execution"): ("read", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/execution/files"):
        ("write", upload_form(kind="document", document_type="dispatch_challan")),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/execution/files/{file_ref}"): ("documents", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/execution/submissions"):
        ("write", {"json": {"stage": "material_dispatched", "text": {"dispatch_reference": "x"}}}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/execution/submissions"): ("read", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/execution/units"): ("read", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/execution/units/check-serial"):
        ("read", {"params": {"serial": "MATRIX-1"}}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/execution/units"): ("write", {"json": UNIT}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/execution/units/bulk"):
        ("write", {"json": {"units": [UNIT]}}),
    ("PATCH", "/api/v1/associate/onboardings/{onboarding_id}/execution/units/{unit_id}"):
        ("write", {"json": {"rating_wp": "545"}}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/execution/units/{unit_id}/void"):
        ("write", {"json": {"reason": "x"}}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/handover"): ("read", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/handover/pdf"): ("documents", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/service-requests"): ("read", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/service-requests"): ("write", SR_FORM),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/service-requests/{request_id}"): ("read", {}),
    ("GET", "/api/v1/associate/payments/instructions"): ("own", {}),
    ("GET", "/api/v1/associate/home"): ("own", {}),
    ("GET", "/api/v1/associate/referral-link"): ("own", {}),
    ("GET", "/api/v1/associate/referrals"): ("own", {}),
    ("GET", "/api/v1/associate/equipment"): ("own", {}),
    ("GET", "/api/v1/associate/commissions"): ("own", {}),
    ("GET", "/api/v1/associate/payouts"): ("own", {}),
    ("GET", "/api/v1/associate/commission-rate"): ("own", {}),
    ("POST", "/api/v1/associate/commissions/{entry_id}/queries"): ("own", {"json": {"message": "x"}}),
    ("GET", "/api/v1/associate/commissions/{entry_id}/queries"): ("own", {}),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/payments"): ("owner", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/payments"): ("owner", {"json": NEFT}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/payments/{payment_id}/proof"): ("owner", upload_form()),
    ("GET", "/api/v1/associate/onboardings/{onboarding_id}/payments/{payment_id}/proof"): ("owner", {}),
    ("POST", "/api/v1/associate/onboardings/{onboarding_id}/payments/{payment_id}/cancel"):
        ("owner", {"json": {"reason": "x"}}),
}


def _money(node) -> set:
    if isinstance(node, dict):
        return (set(node) & MONEY_KEYS) | set().union(*(_money(v) for v in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(_money(v) for v in node)) if node else set()
    return set()


@pytest.fixture
def scenario(client, db_session, onboarding_ready, monkeypatch):
    from app.associates import documents
    monkeypatch.setattr(documents, "render_pdf", lambda html: b"%PDF-1.4 stub")
    from app.memory.models import Associate
    from sqlalchemy import select

    from app.memory.models import CustomerOnboarding, Proposal, ProposalStatus
    owner = make_associate(db_session, capability=AssociateCapability.execution, home_district=ALIPURDUAR)
    territory = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
    sibling = make_associate(db_session, level="district_head", territory=KOLKATA)
    stranger = make_associate(db_session)
    oid = new_customer(client, owner)
    db_session.add(Proposal(lead_id=db_session.get(CustomerOnboarding, oid).lead_id, version=1,
                            status=ProposalStatus.accepted, content={}, total_value=100000.0))
    db_session.commit()
    advance_to(db_session, oid, ProjectStage.agreement_signed)
    payment = client.post(f"/api/v1/associate/onboardings/{oid}/payments", headers=headers(owner),
                          json={**NEFT, "reference": "UTR-MATRIX-SETUP"})
    assert payment.status_code == 201, payment.text
    sub = client.post(f"/api/v1/associate/onboardings/{oid}/execution/submissions",
                      json={"stage": "feasibility_submitted", "text": {"feasibility_reference": "R1"}},
                      headers=headers(owner))
    assert sub.status_code == 201, sub.text
    from scripts import seed_dummy_data
    from app.memory.models import Equipment
    seed_dummy_data.seed(db_session, "development")
    equipment_id = db_session.execute(select(Equipment.id).where(Equipment.model_name == "DUMMY-M540-DCR")).scalar()
    unit = client.post(f"/api/v1/associate/onboardings/{oid}/execution/units", headers=headers(owner),
                       json={**UNIT, "equipment_id": equipment_id, "serial_number": "MATRIX-1"})
    assert unit.status_code == 201, unit.text
    row = db_session.execute(select(Associate).where(Associate.associate_id == owner)).scalar_one()
    values = {"application_type": "associate", "application_id": row.associate_application_id, "document": "photo",
              "associate_id": owner, "level": "districts", "lgd_code": ALIPURDUAR, "seat_id": 999, "flag_id": 999,
              "onboarding_id": oid, "stage": "portal_agreement_approved", "entry_id": "A1", "file_ref": "none",
              "submission_id": sub.json()["submission_id"], "unit_id": unit.json()["unit"]["unit_id"],
              "payment_id": payment.json()["payment"]["payment_id"], "party": "associate",
              "equipment_id": equipment_id, "correction_id": 999, "proposal_id": 999,
              "schedule_id": 999, "batch_id": 999, "query_id": 999, "commission_entry_id": 999999, "request_id": 999999,
              "project_id": 999999, "conversation_id": "0" * 32, "turn_id": "0" * 32}
    return {"owner": owner, "territory": territory, "sibling": sibling, "stranger": stranger, "values": values}


def _fill(path: str, values: dict, method: str) -> str:
    if "/commissions/{entry_id}" in path:   # a commission entry id, not a placeholder-registry id
        values = {**values, "entry_id": values["commission_entry_id"]}
    if path.endswith("/execution/{stage}"):
        values = {**values, "stage": "feasibility_submitted"}
    if "/me/documents/" in path or path.endswith("/associates/{associate_id}/documents/{document}"):
        values = {**values, "document": "id_card"}
    return path.format(**values)


def _call(client, method, path, who, kwargs, values=None):
    if values and "json" in kwargs:
        kwargs = {**kwargs, "json": _resolve(kwargs["json"], values)}
    if "json" in kwargs:   # a distinct payment reference per caller, so duplicates never mask the gate
        tag = (who.get("X-Employee-Role") or who.get("X-Associate-Id") or "x").replace("-", "")
        kwargs = {**kwargs, "json": _resolve(kwargs["json"], {**(values or {}), "role": tag})}
    return client.request(method, path, headers=who, **kwargs)


def _resolve(node, values):
    if isinstance(node, dict):
        return {k: _resolve(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, values) for v in node]
    if node == "{equipment_id}":
        return values["equipment_id"]
    if isinstance(node, str) and "{role}" in node:
        return node.replace("{role}", values.get("role", "x"))
    return node


class TestMatrixIsComplete:
    def test_every_covered_route_has_a_row(self):
        routes = set()
        for name in COVERED_ROUTERS:
            for r in importlib.import_module(f"app.api.routes.{name}").router.routes:
                for method in r.methods:
                    routes.add((method, r.path))
        known = set(STAFF) | set(ASSOCIATE) | set(PUBLIC)
        assert routes == known, f"missing: {sorted(routes - known)}; stale: {sorted(known - routes)}"


@pytest.mark.parametrize("route", sorted(PUBLIC), ids=lambda r: f"{r[0]} {r[1]}")
def test_public_route(client, route):
    method, path = route
    r = client.request(method, path, **PUBLIC[route])
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    # A staff or associate credential changes nothing on a public route.
    assert client.request(method, path, headers=staff("operations"), **PUBLIC[route]).status_code == 200


@pytest.mark.parametrize("route", sorted(STAFF), ids=lambda r: f"{r[0]} {r[1]}")
def test_staff_route(client, scenario, route):
    method, template = route
    allowed, kwargs = STAFF[route]
    path = _fill(template, scenario["values"], method)
    # Refused roles first, so an allowed role's action cannot change what they see.
    for role in sorted(ROLES, key=lambda r: r in allowed):
        r = _call(client, method, path, staff(role), kwargs, scenario["values"])
        if role in allowed:
            assert r.status_code not in (401, 403), f"{role} refused: {r.status_code} {r.text[:200]}"
            if role == "operations" and r.is_success and r.headers.get("content-type", "").startswith("application/json"):
                assert not _money(r.json()), f"money in an Operations response: {_money(r.json())}"
        else:
            assert r.status_code == 403, f"{role} got {r.status_code} {r.text[:200]}"
    # An associate token is never a staff credential.
    assert _call(client, method, path, headers(scenario["owner"]), kwargs, scenario["values"]).status_code == 401


@pytest.mark.parametrize("route", sorted(ASSOCIATE), ids=lambda r: f"{r[0]} {r[1]}")
def test_associate_route(client, scenario, route):
    method, template = route
    kind, kwargs = ASSOCIATE[route]
    path = _fill(template, scenario["values"], method)
    # A staff credential is never an associate one.
    assert _call(client, method, path, staff("admin"), kwargs, scenario["values"]).status_code == 401
    if kind == "own":
        return
    for who in ("stranger", "sibling"):
        r = _call(client, method, path, headers(scenario[who]), kwargs, scenario["values"])
        assert r.status_code == 403, f"{who} got {r.status_code} {r.text[:200]}"
    r = _call(client, method, path, headers(scenario["territory"]), kwargs, scenario["values"])
    if kind == "owner":   # money: the owner only — never a territory Preferred Associate
        assert r.status_code == 403, f"territory Preferred reached a payment route: {r.status_code}"
    elif kind == "documents":  # BE-1 S9: owner only while territory_viewers_see_documents is false
        assert r.status_code == 403, f"territory Preferred opened a document: {r.status_code}"
    elif kind == "read":
        assert r.status_code not in (401, 403), f"territory Preferred refused: {r.text[:200]}"
    else:
        assert r.status_code == 403, f"territory Preferred could write: {r.status_code}"
    r = _call(client, method, path, headers(scenario["owner"]), kwargs, scenario["values"])
    assert r.status_code not in (401, 403), f"owner refused: {r.status_code} {r.text[:200]}"
