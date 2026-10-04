"""HF-A — the public territory lookups say so when the master is not loaded, instead of
answering 200 with an empty list (a blank State dropdown nobody can explain)."""
import pytest

from app.associates import service
from app.associates.service import AssociateError
from app.memory.models import TerritoryState
from tests.associate_support import ALIPURDUAR, WB, territories  # noqa: F401 — fixtures

BASE = "/api/v1/public/territories"
URLS = [f"{BASE}/states", f"{BASE}/states/{WB}/districts", f"{BASE}/districts/{ALIPURDUAR}/blocks"]
NOT_SEEDED = {"detail": {"code": "territories_not_seeded", "message": "Territory data has not been loaded yet."}}


@pytest.mark.parametrize("url", URLS)
def test_unseeded_is_a_503_with_a_named_code(client, url):
    r = client.get(url)
    assert r.status_code == 503 and r.json() == NOT_SEEDED


def test_every_state_inactive_counts_as_unseeded(client, db_session, territories):
    for state in db_session.query(TerritoryState).all():
        state.is_active = False
    db_session.commit()
    for url in URLS:
        assert client.get(url).json() == NOT_SEEDED


def test_seeded_answers_normally(client, territories):
    states = client.get(f"{BASE}/states").json()["items"]
    assert {s["lgd_code"] for s in states} == {WB, 10}
    assert any(d["lgd_code"] == ALIPURDUAR for d in client.get(f"{BASE}/states/{WB}/districts").json()["items"])
    assert client.get(f"{BASE}/districts/{ALIPURDUAR}/blocks").json()["items"]


def test_unknown_parents_stay_404_once_seeded(client, territories):
    assert client.get(f"{BASE}/states/999/districts").status_code == 404
    assert client.get(f"{BASE}/districts/999999/blocks").status_code == 404


def test_registration_still_reports_invalid_territory(db_session):
    """Not changed: resolving a place against an unseeded master is a 422 invalid_territory."""
    with pytest.raises(AssociateError) as exc:
        service.resolve_place(db_session, state_code=WB, district_code=None, block_code=None, field_prefix="",
                              need_district=False, need_block=False)
    assert exc.value.code == "invalid_territory" and exc.value.status_code == 422


def test_the_lookup_limit_is_still_a_429(client, territories):
    from app.api.routes import public_associates
    public_associates._lookup_request_log.clear()
    codes = [client.get(f"{BASE}/states").status_code for _ in range(public_associates._LOOKUP_LIMIT + 1)]
    assert codes[:-1] == [200] * public_associates._LOOKUP_LIMIT and codes[-1] == 429
    public_associates._lookup_request_log.clear()
