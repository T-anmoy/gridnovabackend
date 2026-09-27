"""PIN code ↔ district consistency — a warning, never a rejection.

Data: data/pincodes/ (India Post directory, 2017; see its README for why West Bengal
postal districts map to several current LGD districts)."""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Optional

_DIR = Path(__file__).resolve().parents[2] / "data" / "pincodes"


@lru_cache(maxsize=1)
def _pin_to_lgd_districts() -> dict[str, set[int]]:
    postal = {}
    with (_DIR / "postal_district_map.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            postal[(row["state_lgd_code"], row["postal_district"])] = {
                int(c) for c in row["lgd_district_codes"].split(";")}
    out: dict[str, set[int]] = {}
    with (_DIR / "pincode_districts.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            out.setdefault(row["pincode"], set()).update(postal[(row["state_lgd_code"], row["postal_district"])])
    return out


def check(pin_code: str, district_lgd_code: int) -> Optional[dict]:
    """None when consistent; otherwise a {code, message} warning."""
    districts = _pin_to_lgd_districts().get(pin_code)
    if districts is None:
        return {"code": "pin_not_in_directory",
                "message": f"PIN {pin_code} is not in the PIN directory this system has, so it could not be "
                           "checked against the district. Please double-check it."}
    if district_lgd_code not in districts:
        return {"code": "pin_district_mismatch",
                "message": f"PIN {pin_code} is not normally in the chosen district. Please check the PIN "
                           "and the district."}
    return None
