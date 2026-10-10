"""BE-1 A8 — UTCDateTime always reads back as UTC.

PostgreSQL returns timestamptz values in the session's TimeZone: on a server set to
Asia/Kolkata every timestamp came back as +05:30 (two test_site_location failures
reproduced it). SQLite stored an aware non-UTC value's wall-clock time and dropped the
offset. Values are now converted to UTC when written and normalised when read."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.memory.models import AuditLog, UTCDateTime
from tests.conftest import TEST_DATABASE_URL

IST = timezone(timedelta(hours=5, minutes=30))


def test_bind_converts_and_result_normalises():
    t = UTCDateTime()
    aware_ist = datetime(2026, 10, 10, 9, 0, tzinfo=IST)
    assert t.process_bind_param(aware_ist, None) == datetime(2026, 10, 10, 3, 30, tzinfo=timezone.utc)
    assert t.process_bind_param(datetime(2026, 1, 1, 0, 0), None).tzinfo == timezone.utc
    back = t.process_result_value(aware_ist, None)
    assert back.utcoffset() == timedelta(0) and back == aware_ist
    assert t.process_result_value(datetime(2026, 1, 1), None).tzinfo == timezone.utc
    assert t.process_bind_param(None, None) is None and t.process_result_value(None, None) is None


def test_an_ist_value_round_trips_as_the_same_instant(db_session):
    written = datetime(2026, 10, 10, 9, 0, tzinfo=IST)
    row = AuditLog(entity_type="t", entity_id="1", action="a", performed_by="t", created_at=written)
    db_session.add(row)
    db_session.commit()
    db_session.expire_all()
    read = db_session.get(AuditLog, row.id).created_at
    assert read == written and read.utcoffset() == timedelta(0)


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="PostgreSQL session time zones")
def test_an_asia_kolkata_session_still_reads_utc(db_session):
    db_session.execute(text("SET TIME ZONE 'Asia/Kolkata'"))
    written = datetime(2026, 10, 10, 3, 30, tzinfo=timezone.utc)
    row = AuditLog(entity_type="t", entity_id="2", action="a", performed_by="t", created_at=written)
    db_session.add(row)
    db_session.flush()
    db_session.expire(row)
    read = db_session.get(AuditLog, row.id).created_at
    assert str(db_session.execute(text("SHOW TIME ZONE")).scalar()) == "Asia/Kolkata"
    assert read == written and read.utcoffset() == timedelta(0) and read.isoformat().endswith("+00:00")
    db_session.rollback()
