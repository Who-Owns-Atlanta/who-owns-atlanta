# Project Status — 2026-02-13

## What's in place
- [x] Project directory structure
- [x] Git repo initialized
- [x] `uv` project configured (Python 3.12, `pyproject.toml`)
- [x] Two primary parcel datasets: Fulton County (457MB), DeKalb County (451MB)
- [x] Atlanta Tax_Parcel used for city enrichment (council/NPU/neighborhood linkage via spatial join)
- [x] Reference overlays: city limits, neighborhoods, NPU, council districts, address points, zoning
- [x] Workflow reference docs (two LLM consultations + Horizontal Holdings PDF)
- [x] CLAUDE.md / AGENTS.md for AI assistant guidelines
- [x] Python dependencies (geopandas, psycopg2-binary, sqlalchemy, geoalchemy2, networkx, requests, playwright, rapidfuzz)
- [x] PostgreSQL/PostGIS database (Docker — `woa_postgis` on port 5434)
- [x] Data loading pipeline (GeoJSON → PostGIS: `scripts/01_load_parcels.py`)
- [x] Schema unification (Fulton + DeKalb → `parcels_unified` view) - **FILTERED TO RESIDENTIAL ONLY**
- [x] Corporate/institutional owner flagging (`scripts/02_flag_corporate_owners.py`) - **REFINED FOR PUBLIC AUTHORITIES**
- [x] Address normalization via libpostal (`scripts/03_normalize_addresses.py`, `addr_norm_lookup` table)
- [x] Ownership network/graph logic (`scripts/04_ownership_network.py`, `owner_entities` + `ownership_clusters`)
- [x] SOS name matching — `scripts/08_match_sos.py`
- [x] SOS enrichment of `owner_entities` — `scripts/09_enrich_owners_sos.py`
- [x] SOS network enrichment — `scripts/10_sos_network_enrichment.py` - **FINAL REFINEMENT: Institutional isolation + HOA/Condo land-use codes**
- [x] Atlanta city Tax_Parcel enrichment — `scripts/11_city_enrichment.py`

## What's NOT in place yet
- [ ] Web interface — see `planning/04_web_interface.md`
- [ ] Tests

## Stretch / future
- [ ] Homestead integration — defer until DeKalb coverage is verified
- [ ] Additional Accela record types (code complaints etc.)
- [ ] Metro expansion (Gwinnett, Cobb, Clayton)
- [ ] Corporate ownership choropleth

## Database stats (Post-Refinement)
- **Total Unified Parcels:** 576,170 (Residential focus)
- **Fulton County:** 370,189 total (58,548 corporate, 33,446 institutional)
- **DeKalb County:** 245,766 total (34,092 corporate, 16,892 institutional)
- **Ownership clusters:** ~467,658 total (Post-Institutional Isolation & HOA filtering)
- **Top Clusters (Firm-level):**
    - **Invitation Homes / Tricon Hub:** ~5,020 parcels
    - **Amherst / Pretium (Progress) Hub:** ~3,599 parcels
    - **FirstKey Homes:** ~1,709 parcels
    - **Developer Hub (D.R. Horton etc.):** ~1,386 parcels (Isolated from HOAs)

## Decisions made
- **Primary data:** Fulton County + DeKalb County only. Atlanta city data is redundant (counties cover it).
- **Database:** Docker PostGIS
- **Python:** 3.12 via `uv`
- **libpostal:** Docker container (`woa_libpostal` on port 6789, `clicksend/libpostal-rest`)
- **Pipeline:** Ordered scripts (`scripts/01_load.py`, `02_flag...`, `03_normalize...`, `04_network...`)
- **Owner filtering:** Two flags — `is_corporate` (SOS-resolvable) and `is_institutional` (government, education, trusts, HOAs)
- **Institutional Isolation:** Institutional entities are excluded from forming edges in the ownership graph to prevent "mega-cluster" bridges.
- **GA SOS scraper:** Converted from JS/Puppeteer to Python/Playwright. Cloudflare Turnstile blocks headless. Needs 2captcha or bulk download.

## Database stats
- **Fulton County:** 370,189 parcels (67,719 corporate = 18.3%, 22,620 institutional = 6.1%)
- **DeKalb County:** 245,766 parcels (37,093 corporate = 15.1%, 13,036 institutional = 5.3%)
- **Total:** 615,955 parcels in `parcels_unified` view
- **Owner entities:** 543,421 distinct (name, address, county) groups
- **Ownership clusters:** 471,141 total (post-SOS enrichment), 42,704 with multiple linked entities
- **Address normalization:** 510,849 distinct addresses normalized via libpostal
- **SOS lookups needed:** ~45K distinct corporate owner names

## Key findings

### Out-of-state ownership
| State | Parcels | Owners | Parcels/Owner |
|---|---|---|---|
| Georgia | 76,033 | 38,549 | 2.0 |
| Arizona | 5,275 | 476 | 11.1 |
| Texas | 4,120 | 933 | 4.4 |
| California | 3,829 | 1,130 | 3.4 |
| Florida | 2,798 | 1,260 | 2.2 |
| New York | 2,720 | 824 | 3.3 |
| Connecticut | 458 | 51 | 9.0 |

Arizona and Connecticut have the highest parcels-per-owner ratios — dominated by SFR aggregators (Scottsdale PO box pipeline).

### Portfolio concentration
- 72 entities own 100+ parcels each → 15,223 parcels total
- 183 entities own 50+ parcels → 23,081 parcels
- 0.1% of corporate owners control 17% of corporate-held parcels

### Scottsdale AZ SFR pipeline
All mailing to Scottsdale AZ PO boxes: SFR XII NM ATL OWNER 1 LP (243), STAR 2021 SFR1 BORROWER LP (204), FYR SFR BORROWER LLC (192), HOME SFR BORROWER IV LLC (123), 2018-3 IH BORROWER LP (113, Invitation Homes).

### Network quality
- **Typo catches:** "PROMISE HOMES BORROWER I LLCC" → correct (285 parcels), "LATITIUDE 55 PHARR LLC" variants (123), "GEOGRIA POWER COMPANY" (542)
- **Related entity linking:** ATLER AT BROOKHAVEN variants + VUE AT EMBRY HILLS (233 parcels)
- **Person-behind-LLCs:** Cluster 538 links BAHNHOF LLC + BEAR CREEK FULTON LLC + individual MACGREGOR JOHN M (132 parcels)

## Known issues / observations
- **PO Box collapse (fixed):** libpostal strips PO Box numbers → city/zip-only. Fixed by skipping these in graph.
- **Cluster 2 (7,113 parcels):** Linked via real commercial office addresses (1100 Spring St, etc.). Legitimate but large.
- **Cluster 1 (3,496 parcels):** Large mixed cluster — reduced from 27K after two-pass mega-cluster fix.
- **Cluster 3 (990 parcels):** Subdivision/condo names as owner names (BRANDYWINE, WILDWOOD PARK). Data quirk.
- **"CO" without period:** "GEORGIA POWER CO" in DeKalb not caught by `co\.` pattern — partial match only.

## In place (continued)
- [x] Accela permit records — Building Complaint backfill complete. See `planning/03_accela_records.md`.
  - 10,793 records (2020-01 to 2026-02), 8,109 distinct parcels
  - 3,681 active / 7,112 resolved (status inferred from 36 workflow status values; closedDate not available from search API)
  - 99.3% geometry match, 95.5% Tax_Parcel linkage
  - GIS data: Address_Point (345K), Tax_Parcel (171K) in `gis` schema
  - Script: `scripts/06_pull_accela_records.py` (configurable type, monthly chunking, upsert)
  - Views: `view_records_with_parcels`, `view_records_fulton`, `view_complaint_counts` (with `status_category` = Active/Resolved)

## Next steps
1. Web interface — see `planning/04_web_interface.md` (Phase 1–5)
2. Residential filtering + homestead exemption
3. Additional Accela record types (Code Complaints, etc.) — same script, different `--type`
