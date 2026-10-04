"""PM-A Step 1 — the price list (app/pricing/price_list.py) against Grid Nova's own
figures: all 36 rows of tests/fixtures/price_list_golden_v1.csv to the paisa."""
import csv
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app.pricing import price_list
from app.pricing.price_list import PriceListError, PriceQuote, Unsupported

GOLDEN = Path(__file__).parent / "fixtures" / "price_list_golden_v1.csv"
ROWS = list(csv.DictReader(GOLDEN.open(encoding="utf-8")))


def test_golden_has_36_rows():
    assert len(ROWS) == 36
    assert sum(r["source_status"] == "SUPPLIED" for r in ROWS) == 18


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"{r['package']}-{r['phase']}-{r['system_kw']}")
def test_every_golden_row_to_the_paisa(row):
    q = price_list.quote(row["package"], row["phase"], int(row["system_kw"]))
    assert isinstance(q, PriceQuote)
    assert (q.basic, q.gst, q.final) == (Decimal(row["basic_inr"]), Decimal(row["gst_inr"]), Decimal(row["final_inr"]))
    assert q.source_status == row["source_status"] and q.price_list_version == 1


def test_all_18_mrp_figures_reconcile():
    data = price_list.load()
    count = 0
    for phase, sizes in data["assured_basic_inr"].items():
        for kw, row in sizes.items():
            q = price_list.quote("NOVA_ASSURED", phase, kw)
            assert q.final == Decimal(row["mrp_reference"])
            assert q.final == (Decimal(row["basic"]) * Decimal("1.089")).quantize(Decimal("0.01"))
            count += 1
    assert count == 18


def test_signature_comes_from_assured_basic_not_mrp_and_gst_after_uplift():
    a = price_list.quote("NOVA_ASSURED", "SINGLE", 1)
    s = price_list.quote("NOVA_SIGNATURE", "SINGLE", 1)
    assert s.basic == Decimal("95648.00") == (a.basic * Decimal("1.12")).quantize(Decimal("0.01"))
    assert s.basic != (a.final * Decimal("1.12")).quantize(Decimal("0.01"))      # not from MRP
    assert s.final == Decimal("104160.67")
    # GST after the uplift: Signature's basic excludes GST (it is not the MRP × 1.12),
    # and its GST is charged on that uplifted basic. (Both orders agree to the paisa on
    # every row of v1, so the structure is what is checked, not a differing figure.)
    assert s.gst == price_list.gst_of(s.basic) and s.final == s.basic + s.gst


@pytest.mark.parametrize("package,phase,kw,reason", [
    ("NOVA_ASSURED", "THREE", 1, "three_phase_below_min"),
    ("NOVA_ASSURED", "THREE", 2, "three_phase_below_min"),
    ("NOVA_SIGNATURE", "THREE", 2, "three_phase_below_min"),
    ("NOVA_ASSURED", "SINGLE", 11, "size_above_max"),
    ("NOVA_ASSURED", "THREE", 11, "size_above_max"),
    ("NOVA_ASSURED", "SINGLE", 0, "size_below_min"),
    ("NOVA_ASSURED", "SINGLE", 3.5, "non_integer_size"),
    ("NOVA_ASSURED", "SINGLE", "abc", "non_integer_size"),
    ("NOVA_PLATINUM", "SINGLE", 3, "unknown_package"),
    ("NOVA_ASSURED", "TWO", 3, "unknown_phase"),
])
def test_unsupported(package, phase, kw, reason):
    q = price_list.quote(package, phase, kw)
    assert isinstance(q, Unsupported) and q.reason == reason


def test_whole_float_is_accepted():
    assert price_list.quote("NOVA_ASSURED", "SINGLE", 3.0).kw == 3


def test_document_amount():
    assert price_list.document_amount(Decimal("123456.78")) == "Rs 1,23,456.78"
    assert price_list.document_amount(Decimal("93000.6")) == "Rs 93,000.60"
    assert price_list.document_amount(Decimal("704975.04")) == "Rs 7,04,975.04"
    assert price_list.document_amount(Decimal("12345678.9")) == "Rs 1,23,45,678.90"


class TestLoaderRejectsTampering:
    def _data(self):
        return yaml.safe_load(Path("config/price_list.yaml").read_text(encoding="utf-8"))

    def test_real_file_loads(self):
        data = price_list._validate(self._data())
        assert data["source_sha256"] == "ede4d3dc5e326624a25e4ead787760de8fad4f4d14b887376e7a15fd897ed680"

    def test_extra_size(self):
        data = self._data()
        data["assured_basic_inr"]["THREE"][2] = {"basic": "200000.00", "mrp_reference": "217800.00"}
        with pytest.raises(PriceListError, match="extra: \\[2\\]"):
            price_list._validate(data)

    def test_missing_size(self):
        data = self._data()
        del data["assured_basic_inr"]["SINGLE"][7]
        with pytest.raises(PriceListError, match="missing: \\[7\\]"):
            price_list._validate(data)

    def test_wrong_mrp(self):
        data = self._data()
        data["assured_basic_inr"]["SINGLE"][3]["mrp_reference"] = "201466.00"
        with pytest.raises(PriceListError, match="not the computed Assured final"):
            price_list._validate(data)

    def test_extra_key(self):
        data = self._data()
        data["discount"] = "0.05"
        with pytest.raises(PriceListError, match="extra: \\['discount'\\]"):
            price_list._validate(data)

    def test_changed_rules(self):
        data = self._data()
        data["rules"]["gst"]["effective_rate"] = "0.18"
        with pytest.raises(PriceListError, match="effective_rate"):
            price_list._validate(data)
        data = self._data()
        data["rules"]["signature"]["uplift_rate"] = "0.15"
        with pytest.raises(PriceListError, match="uplift_rate"):
            price_list._validate(data)

    def test_extra_phase_range(self):
        data = self._data()
        data["phase_ranges"]["THREE"]["min_kw"] = 1
        with pytest.raises(PriceListError, match="THREE must list exactly"):
            price_list._validate(data)
