"""BE-1 S3 — nothing synchronous runs on the event loop, and Clerk's JWKS is fetched
through one cached client.

The audit measured /api/v1/health taking 3.4 s while one 4-second chat reply was
generated: chat_message and the auth dependencies were `async def` but called the
synchronous agent, LLM client, database and JWKS fetch, so one slow reply froze every
other request. They are plain functions now (FastAPI runs them in its threadpool).
"""
import asyncio
import inspect
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import dependencies as deps
from app.config import settings
from app.main import app as fastapi_app

SLOW_REPLY_SECONDS = 2.0
HEALTH_BOUND_SECONDS = 0.75


def test_a_slow_chat_reply_does_not_delay_health(monkeypatch):
    from app.llm import client as llm_module

    def slow_reply(*args, **kwargs):
        time.sleep(SLOW_REPLY_SECONDS)  # a blocking model call, as the real client makes
        return "Happy to help — what are you picturing?"

    monkeypatch.setattr(llm_module.llm_client, "generate_reply", slow_reply)

    async def scenario():
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            chat = asyncio.create_task(client.post("/api/v1/chat/message", json={"message": "hi, solar?"}))
            # Timed from BEFORE yielding to the chat task: if the chat blocked the
            # loop, this sleep itself would not return until the slow reply ended.
            started = time.monotonic()
            await asyncio.sleep(0.3)  # the chat request is now inside the slow reply
            health = await client.get("/api/v1/health")
            health_seconds = time.monotonic() - started - 0.3
            chat_response = await chat
        return health, health_seconds, chat_response

    health, health_seconds, chat_response = asyncio.run(scenario())
    assert health.status_code == 200
    assert chat_response.status_code == 200, chat_response.text
    assert health_seconds < HEALTH_BOUND_SECONDS, f"health took {health_seconds:.2f}s behind a slow chat reply"


def test_the_converted_handlers_are_plain_functions():
    from app.api.routes import (
        associate_execution, associate_onboarding, associate_payments, associate_survey, chat, dashboard,
        execution_admin, onboarding_admin, payments_admin, proposal_inputs,
    )
    for fn in (chat.chat_message, deps.get_current_employee, deps.get_current_associate,
               dashboard._require_admin_or_internal_key, proposal_inputs.upload_proposal_input_file,
               associate_onboarding.upload_file, associate_onboarding.upload_signed_agreement,
               onboarding_admin.upload_signed_agreement, associate_execution.upload_file,
               execution_admin.upload_execution_file, associate_payments.upload_proof,
               payments_admin.upload_proof, associate_survey.sign_survey):
        assert not inspect.iscoroutinefunction(fn), fn.__qualname__


# ── JWKS ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def fake_jwks(monkeypatch, rsa_key):
    """PyJWKClient replaced by a counting fake that serves the test key."""
    built = []

    class FakeJWKClient:
        def __init__(self, uri, **kwargs):
            built.append((uri, kwargs))

        def get_signing_key_from_jwt(self, token):
            return type("Key", (), {"key": rsa_key.public_key()})()

    monkeypatch.setattr(jwt, "PyJWKClient", FakeJWKClient)
    monkeypatch.setattr(deps, "_jwks_client", None)
    monkeypatch.setattr(deps, "_jwks_client_uri", None)
    monkeypatch.setattr(settings, "clerk_jwt_issuer", "https://clerk.example.test")
    return built


def _token(rsa_key, **claims) -> str:
    body = {"sub": "user_1", "iss": "https://clerk.example.test", "exp": int(time.time()) + 300, **claims}
    return jwt.encode(body, rsa_key, algorithm="RS256")


def test_the_jwks_client_is_built_once(fake_jwks, rsa_key):
    for _ in range(3):
        deps._verify_clerk_jwt(_token(rsa_key))
    assert len(fake_jwks) == 1
    uri, kwargs = fake_jwks[0]
    assert uri == "https://clerk.example.test/.well-known/jwks.json"
    assert kwargs["cache_jwk_set"] is True and kwargs["lifespan"] > 0 and kwargs["timeout"] <= 10


def test_azp_must_be_an_allowed_origin(fake_jwks, rsa_key, monkeypatch):
    monkeypatch.setattr(settings, "allowed_origins", ["https://www.gridnova.in"])
    assert deps._verify_clerk_jwt(_token(rsa_key, azp="https://www.gridnova.in"))["sub"] == "user_1"
    assert deps._verify_clerk_jwt(_token(rsa_key))["sub"] == "user_1"  # no azp claim: not checked
    with pytest.raises(jwt.InvalidTokenError):
        deps._verify_clerk_jwt(_token(rsa_key, azp="https://evil.example"))


def test_a_token_from_another_origin_is_401(client, fake_jwks, rsa_key, monkeypatch):
    monkeypatch.setattr(settings, "allowed_origins", ["https://www.gridnova.in"])
    token = _token(rsa_key, azp="https://evil.example", public_metadata={"role": "admin"})
    r = client.get("/api/v1/dashboard/leads", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


@pytest.mark.parametrize("route", ["/api/v1/dashboard/leads", "/api/v1/associate/me"])
def test_a_token_without_public_metadata_says_so(client, monkeypatch, route, caplog):
    monkeypatch.setattr(deps, "_verify_clerk_jwt", lambda token: {"sub": "user_1"})
    r = client.get(route, headers={"Authorization": "Bearer x"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "session_token_missing_claims"
    assert any("public_metadata" in rec.getMessage() for rec in caplog.records)
