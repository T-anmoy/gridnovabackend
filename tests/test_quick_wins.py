"""BE-1 S10 — small hardening: no interactive docs in staging/production, finite and
bounded public numbers, rate limits on the remaining public pricing routes."""
import pytest

from app.config import Settings
from app.main import docs_urls

ESTIMATE = "/api/v1/pricing/estimate"
INSTANT = "/api/v1/pricing/instant-estimate"
INTAKE = "/api/v1/public/estimate-request"


@pytest.mark.parametrize("environment, hidden", [("development", False), ("test", False),
                                                 ("staging", True), ("production", True)])
def test_docs_only_outside_production_like(environment, hidden):
    urls = docs_urls(Settings(_env_file=None, environment=environment))
    if hidden:
        assert urls == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    else:
        assert urls == {}


def _raw(client, url, body: str):
    return client.post(url, content=body.encode(), headers={"Content-Type": "application/json"})


@pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN", "1e308", "100000000"])
def test_the_public_estimate_refuses_impossible_sizes(client, value):
    r = _raw(client, ESTIMATE, '{"segment": "residential", "state": "West Bengal", "size_kw": %s}' % value)
    assert r.status_code == 422, r.text


def test_the_instant_estimate_refuses_infinity(client):
    r = _raw(client, INSTANT, '{"segment": "residential", "state": "West Bengal", "sizing_method": "by_bill",'
                              ' "monthly_bill_inr": Infinity}')
    assert r.status_code == 422


@pytest.mark.parametrize("field, value", [("average_bill", "Infinity"), ("roof_area", "NaN"), ("floors", "5000")])
def test_the_estimate_form_refuses_impossible_numbers(client, field, value):
    r = _raw(client, INTAKE, '{"name": "A", "email": "a@example.com", "%s": %s}' % (field, value))
    assert r.status_code == 422


def test_a_real_size_still_prices(client):
    r = client.post(ESTIMATE, json={"segment": "residential", "state": "West Bengal", "size_kw": 3,
                                    "connection_phase": "single_phase"})
    assert r.status_code == 200


@pytest.mark.parametrize("method, url, kwargs", [
    ("POST", ESTIMATE, {"json": {"segment": "residential", "state": "West Bengal", "size_kw": 3}}),
    ("GET", "/api/v1/pricing/packages", {}),
])
def test_public_pricing_routes_are_rate_limited(client, method, url, kwargs):
    codes = [client.request(method, url, **kwargs).status_code for _ in range(21)]
    assert set(codes[:20]) == {200} and codes[20] == 429


def test_packages_is_built_once(client):
    from app.api.routes import pricing
    first = client.get("/api/v1/pricing/packages").json()
    second = client.get("/api/v1/pricing/packages").json()
    assert first == second
    info = pricing._packages_body.cache_info()
    assert info.misses == 1 and info.hits >= 1
