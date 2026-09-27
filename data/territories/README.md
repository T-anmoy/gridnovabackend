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
| `blocks.csv` | `district_lgd_code, block_lgd_code, block_name` | 879 (Bihar 534, West Bengal 345) |

Codes are Local Government Directory (LGD) codes. Names are exactly as LGD
publishes them (mostly upper case, LGD's own spelling); nothing was hand-typed.
The one change from the mirror is Kalimpong (see "Corrections" below).
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

## Cross-checks (West Bengal)

West Bengal's names and codes were checked against three further sources. **Result:
the mirror's names and its code series are right; only Kalimpong needed correcting.**

| Source | What it is | Used to confirm |
|---|---|---|
| **IGOD** — https://igod.gov.in (district pages, e.g. `/district/{id}/blocks`) | Integrated Government Online Directory (NIC / MeitY). Every district page states "Source : Local Government Directory". Plain pages, no CAPTCHA. All 23 West Bengal district pages fetched 2026-09-27 (345 blocks; each page's own "N Results" count matched). | **Spelling**: identical to this data, block for block, in 22 of 23 districts — including all 23 names where the state PDF (below) spells differently. **Kalimpong**: four blocks (Gorubathan, Kalimpong-I, Lava, Pedong), no Kalimpong-II. IGOD publishes no codes. |
| **villagecodes.in snapshot dated 2026-09-02** (sourced from LGD) | Per-block LGD codes. Checked by Grid Nova, not fetched by the code author. | **Codes**: Ondal 2819, Tamluk 2971, Arsha 3044, Alipurduar-I 2919, Mirik 2866, Kalyani 7115 all match this data; none matches the state PDF's series. **Kalimpong**: Gorubathan 2859, Kalimpong-I 2861, Lava 2862, Pedong 7355. |
| **Live LGD portal** — https://lgdirectory.gov.in, "LGD Codes of Development Blocks" | The authority itself (CAPTCHA per lookup). Checked by Grid Nova 2026-09-27. | Code **2862 returns Lava**; code **7354 returns no record**. |

Also cross-checked, and set aside:

- **State portal PDF** — "Blocks Of West Bengal [As per LGD]"
  (https://shgsewb.gov.in/ssp_portal_doc/WBSSP=Code%20List=Block-Code-List.pdf,
  sha256 `d338e521914f2846e54b045768898b2aaef0715ce091d271cff87193da829e06`): 341 blocks
  under a different code series (2154–2494) that shares no code with LGD's current one,
  and 23 spellings that disagree with IGOD. It is an outlier on both counts and was not
  used for anything.
- **An undated third list** (names only, supplied by Grid Nova) matched this data in the
  districts it covered; for Purba Medinipur it followed the state PDF's spellings and
  listed "Contai-II" as well as "Deshopran" (26 names). IGOD and LGD have 25 blocks and
  no Contai-II, so that entry is the block's former name, not a 26th block.
- Bihar: 534 blocks across 38 districts, matching the 534 blocks named in the Bihar
  Education Department's own block list (https://dse.bihar.gov.in/BSS%20contact.pdf).
- Every district code used by the blocks exists in `districts.csv`, and Kolkata (315)
  correctly has no blocks.

## Corrections

- **Kalimpong (district 702).** The 2022 mirror listed code 2862 as "KALIMPONG-II" and
  Lava under 7354. LGD now uses 2862 for Lava, and 7354 no longer exists. `blocks.csv`
  has 2862 = Lava and no 7354 row. A database seeded before this correction is fixed in
  place by migration `0029_kalimpong_lava` (2862 renamed, 7354 switched off — never
  deleted — both audited with the reason above).

## Changing existing codes or names

Never fix a code by editing this file and re-running the seed: the loader matches rows
on LGD code, so a changed code would be inserted as a **second** block while the old
row stayed active (and kept any applications or seats pointing at it). Change codes and
names in place with a migration, as `0029_kalimpong_lava` does, and update this file in
the same change so a fresh database gets the same result.

## Adding a state

Append the state to `states.csv` and its districts and blocks to the other two files,
straight from an LGD download, then re-run `scripts/seed_territories.py`.
