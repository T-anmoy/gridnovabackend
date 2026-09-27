"""Registration field validators (app/associates/validation.py)."""
from datetime import date

import pytest

from app.associates import validation as v
from tests.associate_support import aadhaar


class TestVerhoeff:
    def test_published_worked_example(self):
        # The standard worked example: the check digit for 236 is 3.
        assert v.verhoeff_check_digit("236") == "3"
        assert v.verhoeff_valid("2363")
        assert not v.verhoeff_valid("2364")

    def test_detects_every_single_digit_error(self):
        number = aadhaar()
        assert v.verhoeff_valid(number)
        for i in range(len(number)):
            for d in "0123456789":
                if d != number[i]:
                    assert not v.verhoeff_valid(number[:i] + d + number[i + 1:]), (i, d)

    def test_detects_every_adjacent_transposition(self):
        number = aadhaar("29876543210")
        for i in range(len(number) - 1):
            if number[i] != number[i + 1]:
                swapped = number[:i] + number[i + 1] + number[i] + number[i + 2:]
                assert not v.verhoeff_valid(swapped), i


class TestAadhaar:
    def test_returns_only_the_last_four_digits(self):
        number = aadhaar()
        assert v.aadhaar_last4(number) == number[-4:]
        assert v.aadhaar_last4(f"{number[:4]} {number[4:8]} {number[8:]}") == number[-4:]

    @pytest.mark.parametrize("bad", ["", "12345", "0" + aadhaar()[1:], "1" + aadhaar()[1:], "abcd12345678"])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            v.aadhaar_last4(bad)

    def test_rejects_bad_checksum_without_echoing_the_number(self):
        number = aadhaar()
        bad = number[:-1] + str((int(number[-1]) + 1) % 10)
        with pytest.raises(ValueError) as exc:
            v.aadhaar_last4(bad)
        assert bad not in str(exc.value) and bad[-4:] not in str(exc.value)


class TestPan:
    def test_valid_individual(self):
        assert v.normalize_pan(" abcpe1234f ") == "ABCPE1234F"

    @pytest.mark.parametrize("bad", ["ABCPE1234", "ABCP1E234F", "1BCPE1234F", "ABCPE12345"])
    def test_rejects_bad_shape(self, bad):
        with pytest.raises(ValueError, match="10 characters"):
            v.normalize_pan(bad)

    def test_rejects_unknown_entity_type(self):
        with pytest.raises(ValueError, match="holder type"):
            v.normalize_pan("ABCXE1234F")

    @pytest.mark.parametrize("entity", sorted(v.PAN_ENTITY_TYPES - {"P"}))
    def test_rejects_non_individual_entity_types(self, entity):
        with pytest.raises(ValueError, match="individual"):
            v.normalize_pan(f"ABC{entity}E1234F")


class TestBankAndGst:
    def test_ifsc(self):
        assert v.normalize_ifsc("sbin0001234") == "SBIN0001234"
        for bad in ("SBIN1001234", "SBI00001234", "SBIN000123"):
            with pytest.raises(ValueError):
                v.normalize_ifsc(bad)

    def test_account(self):
        assert v.normalize_bank_account("1234 5678 9012") == "123456789012"
        for bad in ("12345678", "1" * 19, "12345678A"):
            with pytest.raises(ValueError):
                v.normalize_bank_account(bad)

    def test_gstin_format(self):
        assert v.normalize_gstin("19abcpe1234f1z5") == "19ABCPE1234F1Z5"
        for bad in ("19ABCPE1234F1Y5", "19ABCPE1234F0Z5", "ABCPE1234F1Z5", "19ABCPE1234F1Z"):
            with pytest.raises(ValueError):
                v.normalize_gstin(bad)


class TestContactAndAge:
    @pytest.mark.parametrize("raw", ["9876543210", "+91 98765 43210", "09876543210", "91-9876543210"])
    def test_mobile_forms(self, raw):
        assert v.normalize_mobile(raw) == "9876543210"

    @pytest.mark.parametrize("bad", ["5876543210", "987654321", "98765432100"])
    def test_mobile_rejected(self, bad):
        with pytest.raises(ValueError):
            v.normalize_mobile(bad)

    def test_pin(self):
        assert v.normalize_pin("700107") == "700107"
        for bad in ("070010", "70010", "7001077"):
            with pytest.raises(ValueError):
                v.normalize_pin(bad)

    def test_eighteenth_birthday_is_the_boundary(self):
        today = date(2026, 9, 27)
        assert v.check_adult(date(2008, 9, 27), today)
        with pytest.raises(ValueError, match="18"):
            v.check_adult(date(2008, 9, 28), today)
        with pytest.raises(ValueError, match="future"):
            v.check_adult(date(2027, 1, 1), today)

    def test_name_key_ignores_case_and_spacing(self):
        assert v.name_key(v.normalize_name("  ram   kumar ")) == v.name_key("RAM KUMAR")
