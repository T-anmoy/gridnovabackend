"""BE-5 Phase 2 — the handover PDF, the held state and the customer's link."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.handover import config, link
from app.memory.models import AuditLog, HandoverPack
from app.storage import r2
from tests.handover_support import (  # noqa: F401 — fixtures
    ACCOUNTS, ADMIN, MGMT, OPS, catalogue, hand_url, kyc_ready, notifications, onboarding_ready, prepare,
    real_handover_text, record_handover, stub_pdf, territories,
)


@pytest.fixture
def storage(monkeypatch):
    store = {}
    monkeypatch.setattr(r2, "upload_bytes", lambda key, body, ct: store.__setitem__(key, body) or key)
    monkeypatch.setattr(r2, "download_bytes", lambda key: store[key])
    monkeypatch.setattr(r2, "signed_get_url", lambda key, filename=None, as_attachment=False: (
        f"https://signed.example/{key}", __import__("datetime").datetime.now(__import__("datetime").timezone.utc)))
    monkeypatch.setattr("app.config.settings.public_api_base_url", "https://api.example")
    return store


def handed_over(client, db, catalogue, notifications, **kw):
    oid = prepare(client, db, catalogue, **kw)
    assert record_handover(client, oid).status_code == 200
    return oid


def pack_of(db):
    db.expire_all()
    return db.execute(select(HandoverPack)).scalar_one()


class TestHeldWhileDummy:
    def test_shipped_config_holds_the_pack(self, client, db_session, onboarding_ready, catalogue, notifications, storage,
                                           stub_pdf):
        oid = handed_over(client, db_session, catalogue, notifications)
        pack = pack_of(db_session)
        assert pack.pdf_key is None and pack.email_status == "held" and pack.held_reason == "placeholder_content"
        assert notifications.of("handover_customer_email") == []
        assert [p["reason"] for p in notifications.of("handover_blocked")] == ["held"]
        body = client.get(hand_url(oid), headers=OPS).json()
        assert body["pdf"]["status"] == "held"
        assert client.post(hand_url(oid, "/link"), json={}, headers=OPS).json()["detail"]["code"] == "handover_pdf_held"
        assert client.get(hand_url(oid, "/pdf"), headers=OPS).status_code == 404

    def test_om_terms_are_not_exposed_while_dummy(self, client, db_session, onboarding_ready, catalogue, notifications,
                                                  storage, stub_pdf):
        handed_over(client, db_session, catalogue, notifications)
        token = _token(client, db_session)
        # no link could be issued via the API while held; issue directly
        page = client.get(f"/api/v1/public/handover/{token}")
        assert page.status_code == 200
        assert page.json()["warranty"]["om"]["terms_after_free_period"] is None
        assert client.get(f"/api/v1/public/handover/{token}/pdf", follow_redirects=False).status_code == 404


def _token(client, db):
    pack = pack_of(db)
    token = link.issue(db, pack, "test")
    db.commit()
    return token


class TestRendered:
    def test_renders_stores_and_emails(self, client, db_session, onboarding_ready, catalogue, notifications, storage,
                                       stub_pdf, real_handover_text):
        oid = handed_over(client, db_session, catalogue, notifications)
        pack = pack_of(db_session)
        assert pack.pdf_key and pack.pdf_key in storage and pack.email_status == "sent"
        mail = notifications.of("handover_customer_email")
        assert len(mail) == 1 and mail[0]["pack_url"].startswith("https://api.example/api/v1/public/handover/")
        token = mail[0]["pack_url"].rsplit("/", 2)[1]
        assert pack.link_token_hash == link.hash_token(token) and token not in str(pack.__dict__)
        html = storage and __import__("app.handover.pdf", fromlist=["x"]).build_html(
            db_session, db_session.get(__import__("app.memory.models", fromlist=["x"]).CustomerOnboarding, oid), pack)
        assert "Handover pack and warranty certificate" in html and "O&amp;M terms after the first free year to be supplied" not in html
        assert "Annual maintenance plan" in html

    def test_hindi_and_bengali_names_are_escaped_not_dropped(self, client, db_session, onboarding_ready, catalogue,
                                                             notifications, storage, stub_pdf, real_handover_text):
        from app.handover import pdf
        from app.memory.models import Customer, CustomerOnboarding
        oid = handed_over(client, db_session, catalogue, notifications)
        onboarding = db_session.get(CustomerOnboarding, oid)
        pack = pack_of(db_session)
        db_session.get(Customer, onboarding.customer_id).name = "आशा <b>রায়</b>"
        html = pdf.build_html(db_session, onboarding, pack)
        assert "आशा &lt;b&gt;রায়&lt;/b&gt;" in html

    def test_no_customer_email_is_skipped_and_flagged(self, client, db_session, onboarding_ready, catalogue,
                                                      notifications, storage, stub_pdf, real_handover_text):
        handed_over(client, db_session, catalogue, notifications, email="")
        assert pack_of(db_session).email_status == "skipped"
        assert [p["reason"] for p in notifications.of("handover_blocked")] == ["no_customer_email"]


class TestPublic:
    def _ready(self, client, db, catalogue, notifications):
        oid = handed_over(client, db, catalogue, notifications)
        mail = notifications.of("handover_customer_email")[0]
        return oid, mail["pack_url"].rsplit("/", 2)[1]

    @pytest.fixture(autouse=True)
    def _ctx(self, storage, stub_pdf, real_handover_text):
        from app.api.routes import public_handover
        public_handover._read_log._hits.clear()
        public_handover._write_log._hits.clear()

    def test_page_pdf_and_headers(self, client, db_session, onboarding_ready, catalogue, notifications):
        _, token = self._ready(client, db_session, catalogue, notifications)
        page = client.get(f"/api/v1/public/handover/{token}")
        assert page.status_code == 200 and "noindex" in page.headers["x-robots-tag"]
        body = page.json()
        assert body["warranty"]["system"]["years"] == 5 and body["pdf_available"] is True
        assert body["service"]["sla_hours"] is None
        pdf = client.get(f"/api/v1/public/handover/{token}/pdf", follow_redirects=False)
        assert pdf.status_code == 302 and "noindex" in pdf.headers["x-robots-tag"]

    def test_unknown_short_and_revoked_tokens_are_404(self, client, db_session, onboarding_ready, catalogue,
                                                      notifications):
        oid, token = self._ready(client, db_session, catalogue, notifications)
        for bad in ("x" * 43, "short"):
            r = client.get(f"/api/v1/public/handover/{bad}")
            assert r.status_code == 404 and "noindex" in r.headers["x-robots-tag"]
        assert client.post(hand_url(oid, "/link/revoke"), headers=MGMT).status_code == 403
        assert client.post(hand_url(oid, "/link/revoke"), headers=OPS).json()["link"]["state"] == "revoked"
        assert client.get(f"/api/v1/public/handover/{token}").status_code == 404
        assert client.get(f"/api/v1/public/handover/{token}/pdf", follow_redirects=False).status_code == 404

    def test_reissue_replaces_the_old_link(self, client, db_session, onboarding_ready, catalogue, notifications):
        oid, old = self._ready(client, db_session, catalogue, notifications)
        r = client.post(hand_url(oid, "/link"), json={}, headers=ADMIN)
        assert r.status_code == 200 and "/api/v1/public/handover/" in r.json()["link"]["url"]
        assert client.get(f"/api/v1/public/handover/{old}").status_code == 404
        new = r.json()["link"]["url"].rsplit("/", 2)[1]
        assert client.get(f"/api/v1/public/handover/{new}").status_code == 200
        assert len(notifications.of("handover_customer_email")) == 2

    def test_expiry(self, client, db_session, onboarding_ready, catalogue, notifications, monkeypatch):
        monkeypatch.setitem(config.handover(), "link_expiry_days", 30)
        oid, _ = self._ready(client, db_session, catalogue, notifications)
        token = client.post(hand_url(oid, "/link"), json={"send_email": False}, headers=OPS).json()["link"]["url"].rsplit("/", 2)[1]
        assert client.get(f"/api/v1/public/handover/{token}").status_code == 200
        pack = pack_of(db_session)
        pack.link_expires_at = pack.link_expires_at - timedelta(days=31)
        db_session.commit()
        assert client.get(f"/api/v1/public/handover/{token}").status_code == 404

    def test_rate_limited(self, client, db_session, onboarding_ready, catalogue, notifications):
        _, token = self._ready(client, db_session, catalogue, notifications)
        codes = [client.get(f"/api/v1/public/handover/{token}").status_code for _ in range(32)]
        assert codes[:30] == [200] * 30 and codes[30:] == [429, 429]

    def test_link_actions_are_audited(self, client, db_session, onboarding_ready, catalogue, notifications):
        oid, _ = self._ready(client, db_session, catalogue, notifications)
        client.post(hand_url(oid, "/link/revoke"), headers=OPS)
        actions = {a.action for a in db_session.execute(select(AuditLog).where(AuditLog.entity_type == "handover_pack"))
                   .scalars()}
        assert {"link_issued", "link_revoked"} <= actions

    def test_not_handed_over_has_no_link(self, client, db_session, onboarding_ready, catalogue, notifications):
        oid = prepare(client, db_session, catalogue)
        assert client.post(hand_url(oid, "/link"), json={}, headers=OPS).json()["detail"]["code"] == \
            "handover_not_recorded"
