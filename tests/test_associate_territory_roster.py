"""Territory roster — GET /api/v1/associate/territory/members.

A Preferred Associate sees every associate located in their territory and below their
level: the same territory rule as onboarding visibility (app/auth/scoping.py
territory_for_level). A plain Associate is located by their registered address; a
Preferred Associate by the territory they hold."""
from app.memory.models import AssociateStatus, CustomerOnboarding, OnboardingStatus
from tests.associate_support import BHARGAMA, BR, WB
from tests.onboarding_support import (  # noqa: F401 — fixtures
    ADMIN, ALIPURDUAR, ALIPURDUAR_I, ALIPURDUAR_II, ARARIA, ARARIA_BLOCK, BASE, DASH, KOLKATA, headers, kyc_ready,
    make_associate, onboarding_body, onboarding_ready, territories, upload_required_documents,
)

ROSTER = "/api/v1/associate/territory/members"


def _ids(client, gna, **params) -> set:
    r = client.get(ROSTER, params={"page_size": 100, **params}, headers=headers(gna))
    assert r.status_code == 200, r.text
    return {i["associate_id"] for i in r.json()["items"]}


def _onboard(client, gna, n):
    r = client.post(BASE, json=onboarding_body(n=n), headers=headers(gna))
    assert r.status_code == 201, r.text
    return r.json()["onboarding_id"]


class TestAccess:
    def test_plain_associate_is_refused(self, client, db_session, onboarding_ready):
        plain = make_associate(db_session, home_district=ALIPURDUAR, home_block=ALIPURDUAR_I)
        r = client.get(ROSTER, headers=headers(plain))
        assert r.status_code == 403
        assert r.json()["detail"] == "Only Preferred Associates have a territory roster."

    def test_staff_cannot_use_it(self, client, db_session, onboarding_ready):
        assert client.get(ROSTER, headers=ADMIN).status_code == 401

    def test_suspended_preferred_associate_is_refused(self, client, db_session, onboarding_ready):
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR,
                              status=AssociateStatus.suspended)
        assert client.get(ROSTER, headers=headers(head)).status_code == 403


class TestScope:
    def test_district_head_sees_their_district_below_them_and_never_a_sibling(self, client, db_session,
                                                                               onboarding_ready):
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        visible = {
            make_associate(db_session, home_district=ALIPURDUAR, home_block=ALIPURDUAR_I),
            make_associate(db_session, home_district=ALIPURDUAR, home_block=ALIPURDUAR_II),
            make_associate(db_session, home_district=ALIPURDUAR),  # district, no block recorded
            make_associate(db_session, level="block", territory=ALIPURDUAR_I),
            make_associate(db_session, level="block", territory=ALIPURDUAR_II),
        }
        hidden = {
            # Sibling district, same state.
            make_associate(db_session, home_district=KOLKATA),
            make_associate(db_session, level="district_head", territory=KOLKATA),
            # Another state.
            make_associate(db_session, home_district=ARARIA, home_block=ARARIA_BLOCK),
            make_associate(db_session, level="block", territory=ARARIA_BLOCK),
            # Same district, but a peer or above — not below the caller.
            make_associate(db_session, level="district_head", territory=ALIPURDUAR),
            make_associate(db_session, level="state_head", territory=WB),
        }
        seen = _ids(client, head)
        assert seen == visible
        assert not seen & hidden and head not in seen

    def test_state_head_sees_their_state_only(self, client, db_session, onboarding_ready):
        head = make_associate(db_session, level="state_head", territory=WB)
        visible = {
            make_associate(db_session, home_district=KOLKATA),
            make_associate(db_session, home_district=ALIPURDUAR, home_block=ALIPURDUAR_II),
            make_associate(db_session, level="district_head", territory=KOLKATA),
            make_associate(db_session, level="block", territory=ALIPURDUAR_I),
        }
        hidden = {
            # Sibling state: every kind of associate in Bihar.
            make_associate(db_session, home_district=ARARIA, home_block=ARARIA_BLOCK),
            make_associate(db_session, level="state_head", territory=BR),
            make_associate(db_session, level="district_head", territory=ARARIA),
            make_associate(db_session, level="block", territory=ARARIA_BLOCK),
            # The other West Bengal State Head is a peer.
            make_associate(db_session, level="state_head", territory=WB),
        }
        seen = _ids(client, head)
        assert seen == visible
        assert not seen & hidden

    def test_block_level_preferred_sees_plain_associates_in_their_block_only(self, client, db_session,
                                                                              onboarding_ready):
        head = make_associate(db_session, level="block", territory=ALIPURDUAR_I)
        mine = make_associate(db_session, home_district=ALIPURDUAR, home_block=ALIPURDUAR_I)
        hidden = {
            # Sibling block in the same district.
            make_associate(db_session, home_district=ALIPURDUAR, home_block=ALIPURDUAR_II),
            make_associate(db_session, level="block", territory=ALIPURDUAR_II),
            # Same district with no block recorded — not in this block.
            make_associate(db_session, home_district=ALIPURDUAR),
            # A block in another state.
            make_associate(db_session, home_district=ARARIA, home_block=BHARGAMA),
            # Peer in the same block, and the District Head above.
            make_associate(db_session, level="block", territory=ALIPURDUAR_I),
            make_associate(db_session, level="district_head", territory=ALIPURDUAR),
        }
        seen = _ids(client, head)
        assert seen == {mine}
        assert not seen & hidden

    def test_sibling_district_head_sees_the_mirror_image(self, client, db_session, onboarding_ready):
        alipurduar = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        kolkata = make_associate(db_session, level="district_head", territory=KOLKATA)
        a = make_associate(db_session, home_district=ALIPURDUAR, home_block=ALIPURDUAR_I)
        k = make_associate(db_session, home_district=KOLKATA)
        assert _ids(client, alipurduar) == {a}
        assert _ids(client, kolkata) == {k}


class TestPayload:
    def test_item_shape_and_customer_counts(self, client, db_session, onboarding_ready):
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        worker = make_associate(db_session, home_district=ALIPURDUAR, home_block=ALIPURDUAR_I)
        block_pa = make_associate(db_session, level="block", territory=ALIPURDUAR_II)
        verified = _onboard(client, worker, 1)
        _onboard(client, worker, 2)                      # stays draft
        closed = _onboard(client, worker, 3)
        upload_required_documents(client, worker, verified)
        assert client.post(f"{BASE}/{verified}/submit", headers=headers(worker)).status_code == 200
        r = client.post(f"{DASH}/onboardings/{verified}/verification-call",
                        json={"verification_call_completed": True, "note": "Confirmed."}, headers=ADMIN)
        assert r.status_code == 200, r.text
        # An Admin closing a customer after an attribution conflict removes it from the count.
        db_session.get(CustomerOnboarding, closed).status = OnboardingStatus.closed
        db_session.commit()

        body = client.get(ROSTER, headers=headers(head)).json()
        assert body["total"] == 2 and body["page"] == 1 and body["page_size"] == 25
        items = {i["associate_id"]: i for i in body["items"]}
        assert set(items[worker]) == {"associate_id", "full_name", "associate_type", "capability", "level",
                                      "status", "customer_count", "verified_customer_count"}
        assert items[worker] == {
            "associate_id": worker, "full_name": items[worker]["full_name"], "associate_type": "associate",
            "capability": "sales", "level": None, "status": "active", "customer_count": 2,
            "verified_customer_count": 1}
        assert items[worker]["full_name"].startswith("Assoc ")
        assert items[block_pa]["associate_type"] == "preferred_associate"
        assert (items[block_pa]["capability"], items[block_pa]["level"]) == (None, "block")
        assert (items[block_pa]["customer_count"], items[block_pa]["verified_customer_count"]) == (0, 0)

    def test_inactive_members_are_listed_and_filterable(self, client, db_session, onboarding_ready):
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        active = make_associate(db_session, home_district=ALIPURDUAR)
        suspended = make_associate(db_session, home_district=ALIPURDUAR, status=AssociateStatus.suspended)
        block_pa = make_associate(db_session, level="block", territory=ALIPURDUAR_I,
                                  status=AssociateStatus.deactivated)
        assert _ids(client, head) == {active, suspended, block_pa}
        assert _ids(client, head, status="suspended") == {suspended}
        assert _ids(client, head, associate_type="preferred_associate") == {block_pa}
        assert client.get(ROSTER, params={"status": "gone"}, headers=headers(head)).status_code == 422

    def test_pagination(self, client, db_session, onboarding_ready):
        head = make_associate(db_session, level="district_head", territory=ALIPURDUAR)
        members = {make_associate(db_session, home_district=ALIPURDUAR) for _ in range(3)}
        pages = [client.get(ROSTER, params={"page": p, "page_size": 2}, headers=headers(head)).json()
                 for p in (1, 2, 3)]
        assert [len(p["items"]) for p in pages] == [2, 1, 0]
        assert all(p["total"] == 3 for p in pages)
        assert {i["associate_id"] for p in pages for i in p["items"]} == members
        assert client.get(ROSTER, params={"page_size": 101}, headers=headers(head)).status_code == 422
        assert client.get(ROSTER, params={"page": 0}, headers=headers(head)).status_code == 422
