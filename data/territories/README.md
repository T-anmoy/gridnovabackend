# Territory master data (State → District → Block)

Loaded into `territory_states` / `territory_districts` / `territory_blocks` by

    python scripts/seed_territories.py

The loader is idempotent (upsert by LGD code) and never deletes a row: a territory
missing from these files is left as it is, and switching one off is done with its
`is_active` flag.

## Files

| File | Columns | Rows |
|---|---|---|
| `states.csv` | `state_lgd_code, iso_code, state_name` | 2 (Bihar, West Bengal) |
| `districts.csv` | `state_lgd_code, district_lgd_code, district_name` | 61 (Bihar 38, West Bengal 23) |
| `blocks.csv` | `district_lgd_code, block_lgd_code, block_name` | 880 (Bihar 534, West Bengal 346) |

Codes are Local Government Directory (LGD) codes. Names are exactly as LGD
publishes them (upper case, LGD's own spelling); nothing was hand-typed.
`iso_code` is the ISO 3166-2:IN subdivision code without the `IN-` prefix.

## Where the data came from

The official portal (https://lgdirectory.gov.in, "Download Directory") serves these
lists only behind a CAPTCHA, so they were taken from a public mirror of LGD's own
downloads, which keeps LGD's column layout unchanged:

- Repository: https://github.com/planemad/india-local-government-directory
  (README: "Data dump of entities from the India Local Government Directory",
  source https://lgdirectory.gov.in/downloadDirectory.do)
- `administrative/1-state.csv`: commit `0739aa61b36944559772448a37361ba55d7636de` (2022-02-22),
  sha256 `e355f4179744d36813031fa75e08e2e72e9b1009b7c3e4604031ffb909eef500`
- `administrative/2-district.csv`: commit `0aef3c9ac793000f01ab537db04d1b56b9cc6ee3` (2022-12-14),
  sha256 `3adf79c6ad6037eb90ccb1f02c7a39d32e21d5f1368c913fdd975db2d53fc2d7`
- `administrative/blocks.csv`: commit `652c1e9e58e535c0af43745186c5db1d4419105f` (2022-03-11),
  sha256 `1e2e2885d0e92a44d5db1a143a104543a5affc348bee4f453429895bebbb3986`

Rows were filtered to state LGD codes 10 (Bihar) and 19 (West Bengal); columns were
reduced to the ones above. Downloaded 2026-09-27.

## Cross-checks and open questions

- Bihar: 534 blocks across 38 districts, matching the 534 blocks named in the Bihar
  Education Department's own block list (https://dse.bihar.gov.in/BSS%20contact.pdf).
- West Bengal: every district code used by the blocks exists in `districts.csv`, and
  Kolkata (315) correctly has no blocks.
- **Unresolved:** the West Bengal state portal's "Blocks Of West Bengal [As per LGD]"
  list (https://shgsewb.gov.in/ssp_portal_doc/WBSSP=Code%20List=Block-Code-List.pdf,
  sha256 `d338e521914f2846e54b045768898b2aaef0715ce091d271cff87193da829e06`) has 341
  blocks under a different code series (2154–2494). Block names and district codes agree
  one for one with the file used here, which additionally has five newer blocks
  (Kalyani, Lava, Pedong, Banarhat, Kranti) under 2723–3063 / 7115 / 7354 / 7355 /
  7483 / 7484. The LGD Blocks export was used because it is LGD's own download
  format and is the more complete list. Verify against the live LGD portal before
  go-live, and re-run the seed with corrected files if needed.
- The mirror dates from 2022. Any block or district created or renamed in LGD since
  then is not here.

## Adding a state

Append the state to `states.csv` and its districts and blocks to the other two files,
straight from an LGD download, then re-run `scripts/seed_territories.py`.
