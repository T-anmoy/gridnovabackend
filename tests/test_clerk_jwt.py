"""T1 (BE-3): the real Clerk JWT path, end to end.

Nothing here patches _verify_clerk_jwt or PyJWKClient: an RSA key is generated, its
public half is served as a JWKS document by a fake HTTPS fetch (urllib, which PyJWKClient
uses), and real tokens are signed with it. Covers the attacks and mistakes
a production token check must refuse."""
from __future__ import annotations

import base64
import io
import json
import time
import urllib.request

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import app.auth.dependencies as deps
from app.config import settings

ISSUER = "https://clerk.gridnova.test"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"
ORIGIN = "https://www.gridnova.test"
LEADS = "/api/v1/dashboard/leads"
KID = "ins_test_key_1"


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


def _jwk(key, kid: str) -> dict:
    numbers = key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": _b64(numbers.n), "e": _b64(numbers.e)}


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def clerk(monkeypatch, signing_key):
    """A fake Clerk instance: its JWKS served at JWKS_URI, settings pointing at it."""
    fetched = []

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout=None, context=None):
        url = request.full_url if hasattr(request, "full_url") else request
        fetched.append(url)
        assert url == JWKS_URI, f"unexpected fetch {url}"
        return _Response(json.dumps({"keys": [_jwk(signing_key, KID)]}).encode())

    class _Opener:   # PyJWT >= 2.14 fetches through build_opener(...).open(...)
        def open(self, request, timeout=None):
            return urlopen(request, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: _Opener())
    monkeypatch.setattr(deps, "_jwks_client", None)
    monkeypatch.setattr(deps, "_jwks_client_uri", None)
    monkeypatch.setattr(settings, "clerk_jwt_issuer", ISSUER)
    monkeypatch.setattr(settings, "allowed_origins", [ORIGIN, "https://gridnova.test"])
    return fetched


def token(key, *, kid=KID, alg="RS256", **claims) -> str:
    now = int(time.time())
    body = {"sub": "user_2abc", "iss": ISSUER, "iat": now, "nbf": now - 5, "exp": now + 300, "azp": ORIGIN,
            "public_metadata": {"role": "admin"}, "email": "admin@gridnova.test", "name": "A Admin"}
    body.update(claims)
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, key, algorithm=alg, headers={"kid": kid})


def call(client, bearer: str, path: str = LEADS, **headers):
    return client.get(path, headers={"Authorization": f"Bearer {bearer}", **headers})


def test_a_valid_token_authenticates_and_provisions_the_employee(client, clerk, signing_key, db_session):
    r = call(client, token(signing_key))
    assert r.status_code == 200, r.text
    from app.memory.models import Employee
    emp = db_session.get(Employee, "user_2abc")
    assert emp is not None and emp.email == "admin@gridnova.test" and emp.name == "A Admin"
    assert clerk == [JWKS_URI]


def test_the_apex_origin_is_an_allowed_azp_too(client, clerk, signing_key):
    assert call(client, token(signing_key, azp="https://gridnova.test")).status_code == 200


def test_a_token_without_azp_is_accepted(client, clerk, signing_key):
    """Clerk omits azp for some non-browser sessions; when present it must match."""
    assert call(client, token(signing_key, azp=None)).status_code == 200


@pytest.mark.parametrize("case", [
    "expired", "wrong_issuer", "no_issuer", "wrong_signature", "alg_none", "hs256_with_public_key",
    "unknown_kid", "missing_sub", "empty_sub", "no_exp", "wrong_azp", "azp_with_path", "garbage",
])
def test_bad_tokens_are_401(client, clerk, signing_key, case):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = signing_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    bearer = {
        "expired": lambda: token(signing_key, exp=int(time.time()) - 60),
        "wrong_issuer": lambda: token(signing_key, iss="https://evil-clerk.example"),
        "no_issuer": lambda: token(signing_key, iss=None),
        "wrong_signature": lambda: token(other),
        "alg_none": lambda: jwt.encode({"sub": "user_2abc", "iss": ISSUER, "exp": int(time.time()) + 300,
                                        "public_metadata": {"role": "admin"}}, None, algorithm="none",
                                       headers={"kid": KID}),
        "hs256_with_public_key": lambda: _hs256(public_pem),
        "unknown_kid": lambda: token(signing_key, kid="ins_some_other_key"),
        "missing_sub": lambda: token(signing_key, sub=None),
        "empty_sub": lambda: token(signing_key, sub=""),
        "no_exp": lambda: token(signing_key, exp=None),
        "wrong_azp": lambda: token(signing_key, azp="https://evil.example"),
        "azp_with_path": lambda: token(signing_key, azp=f"{ORIGIN}/dashboard"),
        "garbage": lambda: "not.a.jwt",
    }[case]()
    r = call(client, bearer)
    assert r.status_code == 401, f"{case}: {r.status_code} {r.text}"
    assert r.json()["detail"] == "Invalid or expired token."


def _hs256(secret: bytes) -> str:
    """The algorithm-confusion attack: HMAC-sign with the server's PUBLIC key as the secret."""
    import hmac
    import hashlib
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps({
        "sub": "user_2abc", "iss": ISSUER, "exp": int(time.time()) + 300,
        "public_metadata": {"role": "admin"}}).encode()).rstrip(b"=")
    signing_input = header + b"." + body
    sig = base64.urlsafe_b64encode(hmac.new(secret, signing_input, hashlib.sha256).digest()).rstrip(b"=")
    return (signing_input + b"." + sig).decode()


def test_a_verified_token_without_public_metadata_is_403_with_a_code(client, clerk, signing_key):
    r = call(client, token(signing_key, public_metadata=None))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "session_token_missing_claims"


def test_an_unknown_role_is_403(client, clerk, signing_key):
    assert call(client, token(signing_key, public_metadata={"role": "superuser"})).status_code == 403


def test_an_associate_token_works_on_the_portal_and_not_on_staff_routes(client, clerk, signing_key):
    associate = token(signing_key, public_metadata={"principal": "associate", "associate_id": "GNA-WB-26-00001"})
    assert call(client, associate).status_code == 403                       # staff route
    r = call(client, associate, path="/api/v1/associate/me")
    assert r.status_code == 403 and r.json()["detail"] == "Unknown associate."   # verified, then looked up


def test_the_jwks_is_fetched_once_for_many_requests(client, clerk, signing_key):
    for _ in range(3):
        assert call(client, token(signing_key)).status_code == 200
    assert clerk == [JWKS_URI]


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_dev_headers_are_refused_outside_development(client, monkeypatch, environment):
    monkeypatch.setattr(settings, "environment", environment)
    r = client.get(LEADS, headers={"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"})
    assert r.status_code == 403
    r = client.get("/api/v1/associate/me", headers={"X-Associate-Id": "GNA-WB-26-00001"})
    assert r.status_code == 403


def test_dev_headers_cannot_ride_along_a_valid_token_in_production(client, clerk, signing_key, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    r = call(client, token(signing_key), **{"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"})
    assert r.status_code == 403
