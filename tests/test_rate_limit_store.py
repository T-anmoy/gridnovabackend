"""P7 (BE-3): the public rate-limit and cooldown stores are bounded and forget idle keys
(app/security/rate_limit.py), and every public route uses them."""
from __future__ import annotations

import importlib
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.security.rate_limit import RateLimitLog, RecentKeys

T0 = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)


def test_the_window_slides():
    log = RateLimitLog(window_seconds=60)
    assert all(log.hit("1.2.3.4", 3, T0 + timedelta(seconds=i)) for i in range(3))
    assert not log.hit("1.2.3.4", 3, T0 + timedelta(seconds=10))
    assert log.hit("1.2.3.5", 3, T0 + timedelta(seconds=10))          # other keys are independent
    assert log.hit("1.2.3.4", 3, T0 + timedelta(seconds=61))          # the first hit has left the window


def test_a_refused_request_is_not_counted():
    log = RateLimitLog(window_seconds=60)
    for i in range(2):
        log.hit("k", 2, T0 + timedelta(seconds=i))
    for i in range(50):
        assert not log.hit("k", 2, T0 + timedelta(seconds=2 + i * 0.1))
    assert len(log["k"]) == 2


def test_idle_keys_are_forgotten():
    log = RateLimitLog(window_seconds=60)
    for i in range(1000):
        log.hit(f"10.0.{i // 256}.{i % 256}", 5, T0)
    log.hit("fresh", 5, T0 + timedelta(minutes=5))
    assert len(log) == 1 and "fresh" in log


def test_the_number_of_keys_is_capped():
    log = RateLimitLog(window_seconds=60, max_keys=100)
    for i in range(1000):
        log.hit(f"ip-{i}", 5, T0 + timedelta(milliseconds=i))
    assert len(log) == 100
    assert "ip-999" in log and "ip-0" not in log     # the least recently seen went first


def test_recent_keys_expire_and_are_capped():
    recent = RecentKeys(ttl_seconds=60, max_keys=10)
    recent["a@example.com"] = T0
    assert recent.get("a@example.com", T0 + timedelta(seconds=59)) == T0
    assert recent.get("a@example.com", T0 + timedelta(seconds=61)) is None
    for i in range(50):
        recent[f"{i}@example.com"] = T0 + timedelta(seconds=i)
    assert len(recent) == 10


def test_concurrent_hits_never_exceed_the_limit():
    log = RateLimitLog(window_seconds=60)
    allowed = []

    def worker():
        for _ in range(50):
            allowed.append(log.hit("same-ip", 100))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert allowed.count(True) == 100


ROUTES = Path(__file__).resolve().parents[1] / "app" / "api" / "routes"


def test_no_route_keeps_its_own_unbounded_store():
    offenders = [p.name for p in ROUTES.glob("*.py") if "defaultdict(list)" in p.read_text(encoding="utf-8")]
    assert offenders == []


@pytest.mark.parametrize("module, names", [
    ("chat", ["_request_log", "_recent_messages"]),
    ("public_intake", ["_request_log", "_contact_request_log", "_recent_emails", "_recent_contact_emails"]),
    ("public_proposals", ["_accept_request_log", "_pdf_request_log"]),
    ("public_receipts", ["_receipt_request_log"]),
    ("public_booking", ["_availability_request_log", "_booking_request_log", "_recent_emails"]),
    ("public_associates", ["_apply_request_log", "_verify_request_log", "_lookup_request_log"]),
    ("pricing", ["_instant_estimate_request_log", "_estimate_request_log", "_packages_request_log"]),
    ("associate_equipment", ["_request_log"]),
])
def test_every_public_store_is_a_bounded_one(module, names):
    mod = importlib.import_module(f"app.api.routes.{module}")
    for name in names:
        assert isinstance(getattr(mod, name), (RateLimitLog, RecentKeys)), f"{module}.{name}"


def test_the_receipt_link_is_rate_limited(client):
    import app.api.routes.public_receipts as public_receipts
    public_receipts._receipt_request_log["testclient"] = [datetime.now(timezone.utc)] * public_receipts._RATE_LIMIT
    r = client.get("/api/v1/public/receipts/" + "x" * 32 + "/pdf")
    assert r.status_code == 429
