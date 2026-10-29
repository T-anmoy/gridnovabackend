# Access control (BE-17)

Permission bundles, several roles per person, the Network Manager, staff invitations,
safe offboarding, second approval (maker–checker), role-routed alerts, two-step sign-in
(off for every role since BE-21) and regions. BE-21 (Clerk's free plan): offboarding revokes
sessions instead of banning (§4), the bootstrap is locked to named verified addresses (§3),
and an invitation Clerk cannot send still works (`delivery: "manual"`, §3). Written from `config/access_control.yaml`, `app/auth/access.py`,
`app/auth/dependencies.py`, `app/staff/`, `app/api/routes/access_admin.py` and the changed
employee routes in `app/api/routes/dashboard.py`. Base path `/api/v1/dashboard`.

**Errors** on the new routes are structured: `{"detail": {"code", "message", …}}`. The
older employee routes keep their plain-string 403s for "your role cannot …"; their new
refusals (scope, floor, approval) are structured.

---

## 1. The model

- **Permissions** are named actions (`leads.assign`, `payouts.bank_file.export`, …), each
  with a description and a `sensitive` flag. **Roles** are bundles of permissions.
  `config/access_control.yaml` is the single source; it is validated at startup.
- **One person may hold several roles.** Their permissions are the **union**. `role` is
  kept everywhere as the *primary* role (the first held role in the order admin,
  management, network_manager, sales, engineer, operations, accounts, proposal_desk).
- **Exclusive roles.** At most one of **Sales, Engineer, Operations** per person: each
  narrows what its holder sees per record (own leads / technical fields / projects in
  execution), and two together would conflict on the same record. Any of them may be
  combined with any other role. Refused with 422 `role_combination_not_supported`.
- **The database decides.** Every request reads the person's roles from `employee_roles`
  (the token only proves identity). A role change or deactivation therefore takes effect
  **on the person's next request** — not when their token expires. Clerk
  `public_metadata` carries a copy (`roles`, `role`) so the frontend can render before its
  first API call; the backend never reads it, except for the bootstrap Admin path (§3).
- **Admin** = every permission **except** `payouts.bank_file.export`. An Admin who needs
  the bank file also holds **Accounts**.
- Existing roles keep exactly their previous permissions (pinned by
  `tests/test_access_control.py` and the unchanged permission matrices).

### Roles

| Role | Two-step required | May assign | Summary |
|---|---|---|---|
| `admin` | yes | every role | everything except the payout bank file |
| `management` | yes | — | reads everything commercial, manager sign-off, assigns leads |
| `network_manager` | yes | sales, engineer, operations | runs the Associate Network; invites/deactivates staff below Admin; opens any proposal and adjusts its price |
| `sales` | no | — | own + unassigned leads through to a sent proposal |
| `engineer` | no | — | every lead's technical fields; survey review |
| `operations` | no | — | execution and service from agreement signed; never money |
| `accounts` | yes | — | payments, commissions, payouts, the bank file |
| `proposal_desk` | no | — | **BE-18:** the Proposal Studio only — prepares, prices, issues and tracks Studio proposals |

**Network Manager** holds: `leads.view_all` and `leads.view_commercial` (opens any
proposal — detail, document, preview and PDF — with its prices; this also lists every
lead), `pricing.adjust` (sensitive: two-step sign-in), `associates.applications.review`,
`associates.attribution.resolve`, `associates.seats.manage`, `associates.network_summary.view`,
`territories.manage`, `onboardings.view`, `onboardings.view_all` (every associate-sourced
customer, not only own leads), `surveys.view` (no survey prices), `audit.view_associates`,
`staff.manage` (within `can_assign_roles`). No proposal approval, sign-off or sending,
payments, commission rates, company settings, or Admin/Management/Accounts grants.

Note: the network summary it reads contains **aggregate** revenue and commission totals
(the brief lists it among the Network Manager's views).

**Proposal Desk** (BE-18) holds only the Studio's permissions: `studio.view`,
`studio.view_all`, `studio.edit`, `studio.price` (sensitive — but Proposal Desk does not
require two-step, so it is never blocked for this role), `studio.library.manage`,
`studio.convert`. It is **not** exclusive: it may be combined with any role (e.g. Sales). A
person holding only Proposal Desk sees the Studio and "Who can do what"
(`GET /access/matrix`), nothing else: no leads, proposals, payments, projects, Nova Records
project data (`live_project_rule` = none) or archive fields.

| Studio permission | Proposal Desk | Admin | Management |
|---|---|---|---|
| `studio.view` (open the Studio, own proposals) | ✓ | ✓ | ✓ |
| `studio.view_all` | ✓ | ✓ | ✓ |
| `studio.edit` (customers, documents, issue, deliver, revise, duplicate, mark accepted/lost) | ✓ | ✓ | — |
| `studio.price` — sensitive | ✓ | ✓ | — |
| `studio.approve` — sensitive (never the preparer) | — | ✓ | ✓ |
| `studio.library.manage` | ✓ | ✓ | — |
| `studio.convert` (create a lead) | ✓ | ✓ | — |

No other role gained or lost anything. Contract: `docs/api/proposal_studio.md`.

### Gate → permission

Every `AuthenticatedEmployee.can_*()` is one line, `return self.has("<permission>")`:

| Gate | Permission | Gate | Permission |
|---|---|---|---|
| can_approve_standard | proposals.approve_standard | can_view_surveys | surveys.view |
| can_approve_manager_signoff | proposals.manager_signoff | can_review_surveys | surveys.review |
| can_approve_partner_signoff | proposals.partner_signoff | can_clear_reverification | proposals.clear_reverification |
| can_edit_lead | leads.edit | can_verify_onboarding | onboardings.verify |
| can_change_lead_stage | leads.change_stage | can_resolve_attribution | associates.attribution.resolve |
| can_create_lead | leads.create | can_manage_territories | territories.manage |
| can_assign_lead | leads.assign | can_manage_seats | associates.seats.manage |
| can_trigger_proposal_draft | proposals.draft | can_view_payments | payments.view |
| can_send_proposal | proposals.send | can_record_payment | payments.record |
| can_view_audit_logs | audit.view | can_process_payments | payments.process |
| can_manage_employees | staff.manage | can_view_commissions | commissions.view |
| can_view_full_analytics | analytics.view_full | can_process_commissions | commissions.process |
| can_schedule_meeting | meetings.schedule | can_manage_commission_rates | commissions.rates.manage |
| can_adjust_pricing | pricing.adjust | can_view_individual_rates | commissions.individual_rates.view |
| can_query_customer_memory | customers.memory_query | can_export_payout_bank_file | payouts.bank_file.export |
| can_manage_proposal_inputs | proposal_inputs.manage | can_view_network_summary | associates.network_summary.view |
| can_review_proposal_inputs | proposal_inputs.review | can_view_execution | execution.view |
| can_record_project_stage | projects.record_stage | can_verify_execution | execution.verify |
| can_review_associate_applications | associates.applications.review | can_record_execution | execution.record |
| can_view_onboardings | onboardings.view | can_handle_service_requests | service_requests.handle |

The direct role checks that lived in route modules became permissions with the same
holders: `leads.view_all` / `leads.view_assigned` / `leads.view_commercial` /
`leads.view_technical` (lead scope and the Engineer view), `leads.be_assigned`,
`leads.stale_check.run`, `meetings.view` / `meetings.view_all` / `meetings.host`,
`surveys.view_prices`, `execution.view_all` / `execution.view_in_progress` /
`execution.mode.switch` / `execution.admin_override` / `execution.dcr_override`,
`payments.view_all` / `payments.segregation_override`, `payouts.segregation_override`,
`equipment.manage` / `equipment.procurement_price.view`, `placeholders.view` /
`placeholders.resolve`, `archive.manage`, `governance.manage`,
`records.conversations.read_any`, `records.commissions.read`. The full table is
`GET /access/matrix`.

---

## 2. Who can do what

### `GET /access/matrix` — any staff
```json
{"permissions": [{"id": "leads.assign", "description": "Assign a lead to a salesperson.", "sensitive": false}],
 "roles": [{"id": "network_manager", "label": "Network Manager", "description": "…",
            "requires_two_factor": false, "can_assign_roles": ["sales", "engineer", "operations"],
            "permissions": ["associates.applications.review", "…"]}],
 "exclusive_roles": ["sales", "engineer", "operations"]}
```

### `GET /employees/me` — any staff (new fields)
```json
{"id": "user_2ab", "role": "admin", "roles": ["accounts", "admin"], "permissions": ["…"],
 "two_factor_ok": true, "requires_two_factor": false, "can_assign_roles": ["admin", "…"],
 "regions": [], "is_active": true, "google_calendar_connected": false}
```
`role` stays (primary role). Use `permissions` to show or hide actions.

### `GET /employees` — `staff.manage` (Admin, Network Manager)
Each row gains `roles` (list) and `regions` (list; empty = every region). BE-19 adds
`last_sign_in_at` (ISO UTC) and `two_factor_enabled` (bool), read from Clerk
(`GET /v1/users?user_id=…`, cached 10 minutes per user); both **null** when unknown — no Clerk key,
Clerk unreachable, or a person who never signed in to Clerk.

---

## 3. Sign-in and invitations

**First sign-in** (a Clerk user with no employee row):
1. A **pending invitation** matching a **verified** email address of the Clerk user
   (Clerk Backend API `GET /users/{id}`; development/test without a Clerk key: the token's
   `email`) — and, when the user's `public_metadata` carries `staff_invitation_id`, that
   exact invitation — creates the employee with the invited roles. Audited
   `invitation_accepted` and one `role_granted` per role.
2. Else, **bootstrap** (BE-21: locked) — an Admin only when **all** hold:
   `public_metadata.role == "admin"`; **no** `staff_invitation_id`; and one of the Clerk
   user's **verified** addresses (as in 1 — never the token's `email` claim alone) is on
   `BOOTSTRAP_ADMIN_EMAILS` (comma-separated; production: the three first Admins). The
   employee's email is that matched address. Audited `role_granted` and `bootstrap_admin`
   (`performed_by: "bootstrap"`, `after_value: {clerk_user_id, list_match}`). Anything else
   with `role: admin` is 403 `not_invited`, audited `bootstrap_refused` and logged with the
   Clerk user id only (no email). Empty `BOOTSTRAP_ADMIN_EMAILS` = bootstrap off (the
   production boot warns). `scripts/grant_admin.py` is the other way in (RUNBOOK §8).
3. Else **403** `{"code": "not_invited", "message": "You have not been invited to the Grid Nova dashboard. Ask a Grid Nova administrator to invite you."}`.
   An expired invitation: 403 `invitation_expired`. Another staff row already uses the
   address: 403 `email_in_use`. Clerk unreachable for the email check: 503
   `identity_check_unavailable`.

A deactivated employee: 403 `"This account has been deactivated."` (unchanged).

### `POST /staff/invitations` — `staff.manage`
Body `{name, email, roles: [role, …]}` (1–7 roles). Every role must be one you may assign.
- **201** — the invitation (below). Clerk emails the link (`expires_in_days` from config,
  default 7; `redirect_url` = `STAFF_INVITE_REDIRECT_URL`), carrying
  `public_metadata {roles, role, staff_invitation_id}`.
- **202** — an invitation granting **Admin** or **Accounts** waits for a second Admin
  (§5). Nothing is sent until it is approved.
- 403 `role_not_assignable` (`roles` lists the ones outside your scope), 409
  `already_staff` / `invitation_exists`, 422 `invalid_email` / `invalid_name` /
  `role_combination_not_supported`.
- **Clerk refuses or is unreachable** (BE-21): the invitation is **still created** and
  pending, with `clerk_status: "failed"`, `delivery: "manual"` and `manual_instructions`
  ("Ask {name} to sign up at {FRONTEND_BASE_URL}/dashboard/sign-up with {email}."). The
  person signs up normally with that address; at first sign-in the verified-email match
  (§3 step 1) makes them the employee with exactly the invited roles. (Before BE-21 this
  was 502 `clerk_invitation_failed`, nothing saved — that error no longer exists.)

```json
{"invitation_id": "9f1c…", "name": "Abhijit Bhattacharjee", "email": "abhijit@gridnova.in",
 "roles": ["network_manager"], "status": "pending", "invited_by": "user_2ab", "clerk_status": "sent",
 "delivery": "clerk_email", "manual_instructions": null, "created_at": "…", "expires_at": "…", "resent_at": null, "accepted_employee_id": null,
 "accepted_at": null, "revoked_by": null, "revoked_at": null}
```
`status`: `pending` | `accepted` | `revoked` | `expired` (a pending invitation past
`expires_at` reads as `expired`). `clerk_status`: `sent` | `skipped` (no Clerk key) |
`failed` (Clerk refused, BE-21). `delivery`: `clerk_email` (`clerk_status` sent) |
`manual` (anything else — nobody emailed the person). `manual_instructions`: the sentence
for the dashboard while a manual invitation is pending, else null.

### `GET /staff/invitations?status=` — `staff.manage`
`{"items": [invitation, …]}`, newest first.

### `POST /staff/invitations/{id}/resend` — `staff.manage`, within scope
Pending or expired only. Clerk has no resend: the old Clerk invitation is revoked and a new
one created; `expires_at` restarts. If Clerk refuses the new one the invitation stays
pending with `delivery: "manual"`. 409 `invitation_not_pending` / `invitation_exists`.

### `DELETE /staff/invitations/{id}` — `staff.manage`, within scope
Revoked here and in Clerk. Response = the invitation + `clerk_error` (null, or why Clerk's
revoke failed — the invitation is still refused at sign-in).

---

## 4. Roles, deactivation, handover

**Scope rule** for every change below: you may act only on people whose roles — before
and after — are all within your `can_assign_roles` (Network Manager: Sales, Engineer,
Operations; Admin: all). Else 403 `role_not_assignable`.

### `PUT /employees/{id}/roles` — `staff.manage`
Body `{roles: [role, …]}` — the complete new set.
- **200** `{"employee_id", "roles", "role", "previous_roles", "clerk_synced"}` (plus
  `"unchanged": true` when nothing changed). Clerk is updated first; 502
  `clerk_sync_failed` changes nothing.
- **202** `{"status": "pending_approval", "message", "approval_request"}` when the change
  grants or removes **Admin** or **Accounts** (config switches, §5).
- 409 `own_admin_role` (you cannot remove your own Admin role), 409 `admin_floor` (fewer
  than `staff.min_active_admins` — default **2** — active Admins would remain), 422
  `role_combination_not_supported`, 404 `employee_not_found`.

### `POST /employees/{id}/role` — unchanged shape, new rules
`{role}` → the person holds exactly that role. Same scope, floor and second-approval rules
(202 as above). 200 keeps `{"employee_id", "role", "clerk_synced", "local_mirror": true}`
and adds `roles`. An unknown employee is now 404 (it used to update Clerk only).

### `PATCH /employees/{id}/deactivate` — `staff.manage`, within scope
409 `self_deactivation`, 409 `admin_floor`. 200:
```json
{"employee_id": "user_9", "is_active": false, "clerk_banned": false, "sessions_revoked": 2,
 "invitation_revoked": false, "clerk_error": null, "clerk_mode": "revoke_sessions",
 "handover": {"open_leads": [{"lead_id": 12, "pipeline_stage": "qualified"}],
              "open_proposals": [{"proposal_id": 4, "lead_id": 12, "status": "draft"}],
              "pending_invitations": [{"invitation_id": "…", "email": "…"}]}}
```
The database refuses the person from their next request — even with a token Clerk still
considers valid. Then Clerk, by `CLERK_BAN_ENABLED` (BE-21):

| `clerk_mode` | When | Clerk calls | `clerk_banned` | `sessions_revoked` |
|---|---|---|---|---|
| `revoke_sessions` | `CLERK_BAN_ENABLED=false` (default; Clerk free plan has no ban) | `GET /v1/sessions?user_id=…&status=active`, then `POST /v1/sessions/{id}/revoke` for each | `false` | how many were revoked (`null` without a Clerk key) |
| `ban` | `CLERK_BAN_ENABLED=true` (Clerk Pro) | `POST /v1/users/{id}/ban` (Clerk revokes every session, refuses sign-in) | `true` | `null` (Clerk does not say how many) |

`invitation_revoked` is always `false` here (kept so staff and associate responses share
one shape). A Clerk failure never undoes the deactivation: it returns `clerk_error` (and,
for a partial revoke, the count that did succeed). With sessions revoked the person can
still sign in to Clerk, and the API refuses them. Audit: `deactivated`, then
`sessions_revoked` (the outcome) — or `clerk_offboarding_failed` when Clerk failed.
**Type change:** `sessions_revoked` was a boolean before BE-21; it is now `int | null`. The leaver's own pending approval requests are withdrawn (`approval_requests_withdrawn`: their ids).

### `PATCH /employees/{id}/reactivate` — `staff.manage`, within scope
Roles unchanged. 200 adds `roles`, `clerk_unbanned`, `clerk_error`, `clerk_mode`. Only
`clerk_mode: "ban"` has anything to undo (`POST /v1/users/{id}/unban`, audited
`clerk_unbanned`); with `revoke_sessions` nothing changes in Clerk — the database decides
(`clerk_unbanned: false`).

### `GET /employees/{id}/handover` — `staff.manage`
`{"employee_id", "open_leads", "open_proposals", "pending_invitations"}` (as above).

### `POST /employees/{id}/reassign` — `staff.manage` **and** `leads.assign`
Body `{to_employee_id}`. Moves every open (not won/lost) lead to an active employee who
takes leads (Admin or Sales). 200 `{"from_employee_id", "to_employee_id", "lead_ids",
"moved"}`. 422 `not_assignable` / `same_employee`. Each lead is audited `reassigned`; the
batch `leads_reassigned`.

### `PUT /employees/{id}/regions` — `staff.manage`, within scope
Body `{regions: ["West Bengal", …]}` (TerritoryState names; `[]` = every region).
422 `unknown_region`. Applied to associate lists and **every** per-associate and
per-application route (detail, KYC document links, status, approve, invitation, documents,
reassign-customers — BE-19) by `state_id`: outside → 403 `"This record is outside the regions
you work in."`.

**Leads (BE-19):** `GET /leads` and `GET /leads/{id}` are region-scoped too. A lead's state is
its associate onboarding's site state when it has one, else the project's `state` text when it
is exactly a state's name (any case, spaces trimmed). A lead whose state is unknown — no
onboarding and no or unrecognised project state (most chat and web leads) — stays visible to
everyone, because hiding it would hide it from every regional employee. Outside → the list
leaves it out (`total` too); the detail answers 403 as above. No change for anyone whose
`regions` is empty.

---

## 5. Second approval (maker–checker)

`config/access_control.yaml` `approvals.require_second_approval`:

| Switch | Default | What waits |
|---|---|---|
| `role.admin` | on | granting or removing Admin (role change or invitation) |
| `role.accounts` | on | granting or removing Accounts |
| `commissions.standard_rate_publish` | off | `POST /commission-rate-schedules` |
| `commissions.individual_rate_change` | off | `POST /associates/{id}/commission-rate` |
| `payouts.batch_approve` | off | `POST /payout-batches/{id}/approve` |

While a request is pending nothing changes; the route answers **202**
`{"status": "pending_approval", "message": "Waiting for a second approver. …", "approval_request": {…}}`.
Eligible approvers are emailed (`approval_requested`). Requests expire after
`approvals.expiry_hours` (default 72).

The approver must be a **different person** holding the permission (`staff.manage` with
the roles in scope for role changes and invitations; `commissions.rates.manage` for rates;
`commissions.process` for payout batches). On approval the change is re-checked with the
approver's permissions (scope, Admin floor, commission rules); a refusal leaves the request
pending. A role change whose target's roles moved on since the request: 409 `approval_stale`.

`GET /approvals` answers `{"items": [ApprovalRequest, …]}` (newest first). Each item:
```json
{"approval_request_id": "…", "action": "staff.set_roles", "summary": "Change Raju Paul's roles from sales to admin, sales",
 "payload": {"employee_id": "…", "roles": ["admin", "sales"], "before": ["sales"], "switches": ["role.admin"]},
 "status": "pending", "requested_by": "user_2ab", "requested_by_name": "Tanmoy Halder", "created_at": "…", "expires_at": "…",
 "decided_by": null, "decided_at": null, "decision_note": null, "result": null}
```
`action`: `staff.set_roles` | `staff.invite` | `commissions.standard_rate_publish` |
`commissions.individual_rate_change` | `payouts.batch_approve`. `status`: `pending` |
`approved` | `rejected` | `cancelled` | `expired`.

| Route | Who | Notes |
|---|---|---|
| `GET /approvals?status=` | holders of any approver permission | `{items}`: requests you asked for, or that you could decide — BE-19: a staff change (`staff.*`) only when you may assign **every** role it touches, so the Network Manager no longer sees Admin or Accounts grants it would be refused. `requested_by_name` (display name; null if unknown) on every item and in the approve / reject / cancel answers |
| `POST /approvals/{id}/approve` `{note?}` | a different eligible person | 403 `self_approval` / `not_an_approver`, 409 `approval_not_pending` / `approval_expired` / `approval_stale` |
| `POST /approvals/{id}/reject` `{note?}` | same | |
| `POST /approvals/{id}/cancel` | the requester | 403 `not_requester` |

---

## 6. Alerts by role

`GET /access/admins` — any staff (BE-19): the active Admins, so "ask an Admin" can name them.
```json
{"items": [{"id": "user_…", "name": "Ada Admin", "email": null}]}
```
Sorted by name. `email` is filled only for a viewer who is an Admin; null for everyone else.

`GET /access/alerts` — any staff:
```json
{"events": [{"event": "payment_recorded", "roles": ["accounts"], "assigned_employee": false, "owner": false,
             "active_holders": {"accounts": 1}, "fixed_addresses": 1, "reaches_someone": true}]}
```
BE-19: a viewer holding `staff.manage` (Admin, Network Manager) also gets, per event,
`"addresses": {"fixed": ["info@gridnova.in"], "by_role": {"admin": ["a@gridnova.in"], …}}` — the
addresses actually emailed today (deactivated people left out). The lead's assigned employee and
a Studio owner vary per record and are not listed. Everyone else sees counts only, as before.
New events: `proposal_ready_for_review` (Admin + the lead's assigned employee: a drafted proposal
waits for approval — after a formal proposal is prepared, or a site assessment approve-and-draft)
and `studio_converted` (Sales: an unassigned lead made from a Studio proposal).

Recipients of a staff email = the fixed addresses configured before (`STAFF_NOTIFY_EMAILS`,
`ACCOUNTS_NOTIFY_EMAILS`, `OPERATIONS_NOTIFY_EMAILS` — shared mailboxes keep receiving)
**plus** every active employee holding one of the event's roles **plus** the lead's
assigned employee where `assigned_employee: true`. Deduplicated; a deactivated employee is
never emailed (even when their address is also a fixed one). An event that reaches nobody
logs one warning per event type. New event: `associate_application_submitted` (Network
Manager, Admin).

BE-18 adds `owner: true` — the Studio proposal's owner receives the event, and no shared
mailbox does: `studio_viewed`, `studio_accepted`, `studio_declined`, `studio_expiring`,
`studio_not_viewed`. `studio_approval_requested` goes to Admin and Management (the holders of
`studio.approve`) and the staff mailbox.

---

## 7. Two-step sign-in

**Off for every role** (BE-21, the owner's decision of 29 Oct 2026). Grid Nova is on
Clerk's free plan, where two-step sign-in is not available, so `requires_two_factor` is
`false` for all eight roles in `config/access_control.yaml`: nobody gets
`two_factor_required` today, and `GET /employees/me` / `GET /access/matrix` report
`requires_two_factor: false`. `two_factor_ok` is still reported (from `fva`).

**Turning it back on** (after buying Clerk Pro and enabling the authenticator app): each
person enables two-step in their Clerk account, then their role's
`requires_two_factor` is set to `true` in the config — one line per role, no code change.
From then on the rule below applies to that role.

The rule, for any role with `requires_two_factor: true`: a request
that uses a **sensitive** permission is refused with
**403** `{"code": "two_factor_required", "message": "This action needs two-step verification. …"}`
unless the session token's `fva` claim shows a completed second factor (`fva[1] >= 0`).
A sensitive permission that one of the person's roles grants *without* requiring two-step
(e.g. `pricing.adjust` via Sales) is not blocked. Non-sensitive pages stay open, so the
person can reach their account settings. `GET /employees/me` reports `two_factor_ok` and
`requires_two_factor`.

Sensitive: proposals.manager_signoff, proposals.partner_signoff, pricing.adjust,
associates.applications.review, associates.attribution.resolve, associates.seats.manage,
territories.manage, execution.mode.switch, execution.admin_override, execution.dcr_override,
payments.process, payments.segregation_override, commissions.process,
commissions.rates.manage, payouts.segregation_override, payouts.bank_file.export,
placeholders.resolve, archive.manage, governance.manage, staff.manage, studio.price,
studio.approve. (`studio.price` and `studio.approve` were already sensitive in
`config/access_control.yaml` since BE-18; this list now says so.)

Development/test headers count as two-step done; `X-Employee-Two-Factor: off` simulates a
session without it. `X-Employee-Roles: admin,accounts` signs in with several roles.

---

## 8. Migration 0056_access_control

`employee_roles` (employee_id, role, granted_by, granted_at; PK both) backfilled with one
row per employee from `employees.role` (`granted_by: migration_0056`); `employees.role`
stays as the primary role. `employees.regions` (JSON, NULL = all). `staff_invitations`
(one pending per email, partial unique index). `approval_requests`. PostgreSQL
`employeerole` gains `network_manager` (left in place on downgrade; the downgrade refuses
while anyone holds it as their primary role).

## 9. For the frontend (FE-17)

- Drive menus and buttons from `GET /employees/me` → `permissions` (not `role`).
- Handle 202 `pending_approval` on role changes, invitations and (when switched on) the
  three commission actions; show `approval_request.summary` and "waiting for a second Admin".
- Handle 403 `two_factor_required` by sending the person to Clerk's two-step settings.
- Handle 403 `not_invited` / `invitation_expired` on the sign-in landing page.
- The staff invitation lands on `STAFF_INVITE_REDIRECT_URL`; with a custom redirect,
  Clerk requires that page to complete the sign-up (`<SignUp />` with the ticket).

### For the frontend (FE-21)

- `GET /employees/me` → `requires_two_factor` is `false` for everyone: hide the two-step
  prompts (keep the `two_factor_required` handler — it returns if the config is flipped).
- Deactivate dialog: read `clerk_mode`. `revoke_sessions` + no `clerk_error` = "every open
  session has ended"; `ban` + `clerk_banned` = blocked in Clerk too. Show the "do it by
  hand in Clerk" warning only when `clerk_error` is set. `sessions_revoked` is now a
  number (or null), not a boolean.
- Invitations: show `manual_instructions` when `delivery == "manual"` (copy button).
  `clerk_status` may now be `failed`.
