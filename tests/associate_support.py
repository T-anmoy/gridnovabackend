"""Shared fixtures and builders for the associate-programme tests.

Territories are a subset of the REAL rows in data/territories/ (copied row for row
into a temporary directory and loaded through the real loader) — small enough to
seed per test on PostgreSQL, and never an invented code.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

import pytest

from app.associates import service, validation
from app.associates.territory_seed import DATA_DIR, seed_territories

# Real LGD codes (see data/territories/).
WB, BR = 19, 10
KOLKATA = 315                      # West Bengal, no blocks
ALIPURDUAR = 664                   # West Bengal
ALIPURDUAR_I, ALIPURDUAR_II = 2919, 2920
ARARIA = 188                       # Bihar
ARARIA_BLOCK, BHARGAMA = 1624, 1625

_SUBSET_DISTRICTS = {KOLKATA, ALIPURDUAR, ARARIA}
_SUBSET_BLOCKS = {ALIPURDUAR_I, ALIPURDUAR_II, ARARIA_BLOCK, BHARGAMA}


def write_subset(target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for name, keep in (
        ("states.csv", lambda r: True),
        ("districts.csv", lambda r: int(r["district_lgd_code"]) in _SUBSET_DISTRICTS),
        ("blocks.csv", lambda r: int(r["block_lgd_code"]) in _SUBSET_BLOCKS),
    ):
        with (DATA_DIR / name).open(encoding="utf-8", newline="") as src:
            reader = csv.DictReader(src)
            rows = [r for r in reader if keep(r)]
            fields = reader.fieldnames
        with (target / name).open("w", encoding="utf-8", newline="") as dst:
            writer = csv.DictWriter(dst, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    return target


@pytest.fixture
def territories(_isolated_db, tmp_path):
    session = _isolated_db()
    try:
        seed_territories(session, write_subset(tmp_path / "territories"))
        session.commit()
    finally:
        session.close()


@pytest.fixture
def kyc_ready(monkeypatch, fake_storage, territories):
    """Everything a registration needs: territories, an encryption key, storage."""
    from app.config import settings
    from app.security.encryption import generate_key

    monkeypatch.setattr(settings, "fernet_encryption_key", generate_key())
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.gridnova.test")
    return fake_storage


def aadhaar(body11: str = "23456789012") -> str:
    """A 12-digit number that passes the Verhoeff check (not a real person's)."""
    return body11 + validation.verhoeff_check_digit(body11)


def adult_dob(years: int = 30) -> str:
    today = service.today_ist()
    return date(today.year - years, 1, 15).isoformat()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"%" * 64


def real_jpeg() -> bytes:
    """A genuine decodable JPEG, for tests that render it into a PDF."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (60, 75), (40, 90, 60)).save(buf, "JPEG")
    return buf.getvalue()


def files(photo: bytes = JPEG) -> dict:
    return {
        "masked_aadhaar": ("aadhaar.pdf", PDF, "application/pdf"),
        "pan_card": ("pan.png", PNG, "image/png"),
        "photo": ("photo.jpg", photo, "image/jpeg"),
        "cancelled_cheque": ("cheque.jpg", JPEG, "image/jpeg"),
    }


def common_form(n: int = 1, **overrides) -> dict:
    """Valid common KYC fields. `n` varies every identity attribute so several
    applicants can register in one test."""
    from app.associates import texts

    version, _ = texts.current_consent()
    data = {
        "full_name": f"Test Applicant {n}",
        "date_of_birth": adult_dob(),
        "address_line": "1 Test Road",
        "state_lgd_code": str(WB),
        "district_lgd_code": str(ALIPURDUAR),
        "block_lgd_code": str(ALIPURDUAR_I),
        "pin_code": "736121",
        "mobile": f"98765{43210 + n:05d}"[-10:],
        "email": f"applicant{n}@example.test",
        "aadhaar_number": aadhaar(f"2345678{9000 + n:04d}"),
        "pan": f"ABCPE{1000 + n:04d}F",
        "bank_account_number": f"1234567{n:05d}",
        "bank_ifsc": "SBIN0001234",
        "kyc_consent_version": version,
        "kyc_consent_accepted": "true",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def associate_form(n: int = 1, **overrides) -> dict:
    return common_form(n, **{"capability": "sales", **overrides})


def preferred_form(n: int = 1, level: str = "district_head", **overrides) -> dict:
    data = common_form(n)
    data.update({
        "requested_level": level,
        "requested_state_lgd_code": str(WB),
        "requested_district_lgd_code": str(ALIPURDUAR),
        "requested_block_lgd_code": str(ALIPURDUAR_I),
        "business_type": "Proprietorship",
        "office_address": "2 Office Lane",
        "prior_experience": "Five years in rooftop solar installation.",
        "team_size": "4",
        "reference_1_name": "Ref One",
        "reference_1_mobile": f"91234{56000 + n:05d}",
        "reference_2_name": "Ref Two",
        "reference_2_mobile": f"81234{56000 + n:05d}",
    })
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


ASSOCIATE_APPLY = "/api/v1/public/associates/apply"
PREFERRED_APPLY = "/api/v1/public/preferred/apply"
ADMIN = {"X-Employee-Id": "emp-admin-1", "X-Employee-Role": "admin"}
DASH = "/api/v1/dashboard"


def submit(client, url: str, form: dict, photo: bytes = JPEG):
    return client.post(url, data=form, files=files(photo))


def approve_flow(client, application_type: str, application_id: int, note: str = "Called; identity confirmed."):
    base = f"{DASH}/associate-applications/{application_type}/{application_id}"
    r = client.post(f"{base}/status", json={"status": "under_review"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return client.post(f"{base}/approve", json={"verification_call_completed": True,
                                                 "verification_call_note": note}, headers=ADMIN)
