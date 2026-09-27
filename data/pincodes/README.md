# PIN code → district (West Bengal and Bihar)

Used only for a **non-blocking warning** on associate onboarding: "this PIN code is not
normally in the district you chose" (app/onboarding/pincode.py). It never rejects a
customer.

## Files

| File | Columns | Rows |
|---|---|---|
| `pincode_districts.csv` | `pincode, state_lgd_code, postal_district` | 2,196 (2,032 distinct PINs; 152 PINs span more than one postal district) |
| `postal_district_map.csv` | `state_lgd_code, postal_district, lgd_district_codes, lgd_district_names` | 58 |

## Where the data came from

India Post's "All India Pincode Directory" as published on data.gov.in. The official
download (https://www.data.gov.in/files/ogdpv2dms/s3fs-public/dataurl03122020/pincode.csv)
could not be fetched by a script (it redirects to itself), so it was taken from a public
mirror of the same directory:

- https://github.com/saravanakumargn/All-India-Pincode-Directory,
  file `all-india-pincode-html-csv.csv`, last commit 2017-01-31 (`0036992e9823`),
  sha256 `d7fe0128546f2328fb508d249016549a6e68a75da035349ec42f67edc8cd8de3`
- Filtered to `statename` WEST BENGAL and BIHAR; kept the distinct
  (pincode, district) pairs. Downloaded 2026-09-27.

## Known limitation — the directory predates four West Bengal districts

The directory lists West Bengal's districts as they were before Alipurduar (2014),
Kalimpong, Jhargram, and the Purba/Paschim Bardhaman split (2017): 20 postal districts
against LGD's 23. `postal_district_map.csv` therefore maps each postal district to
**every** current LGD district that took territory from it:

| Postal district (2017 data) | Accepted LGD districts |
|---|---|
| BARDHAMAN | Purba Bardhaman, Paschim Bardhaman |
| DARJILING | Darjeeling, Kalimpong |
| JALPAIGURI | Jalpaiguri, Alipurduar |
| WEST MIDNAPORE | Paschim Medinipur, Jhargram |
| MEDINIPUR (undivided name on some offices) | Purba Medinipur, Paschim Medinipur, Jhargram |

These successor relationships are the code author's knowledge, not something the
directory states. They are used only to **widen** what counts as consistent, so an
error in them can only suppress a warning — never block a customer or raise a false
one. Bihar's 38 postal districts match LGD's 38 one to one; three differ only in
spelling (AURANGABAD(BH), EAST/WEST CHAMPARAN ↔ PURBI/PASHCHIM CHAMPARAN).

A PIN missing from the directory produces its own "cannot check this PIN" warning,
not a mismatch. Replace these files with a current LGD "Villages with PIN codes"
extract when one can be obtained.
