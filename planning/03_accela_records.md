# Plan: Accela Permit Records Integration

**Created:** 2026-02-18
**Status:** Approved — in progress

## Goal

Pull Atlanta building complaint records (and later other record types) from the Accela API into our PostGIS database. These are per-parcel permit records that will augment the ownership network — linked via parcel ID in reporting later. This work is **independent/parallel** to the SOS data effort.

## Reference

- Working Accela integration: `git@github.com:jessedp/nbh_accela.git` (cloned to `/tmp/nbh_accela`)
- Custom `accela` Python package: `https://github.com/jessedp/accela.git`
- OpenAPI specs: `nbh_accela/docs/openapi/v4-records.json`, `v4-search.json`
- Accela creds already in `.env` (root of this project)

## API Approach

Two viable methods in the `accela` package:

1. **`records.list()`** — `GET /v4/records` with `type` query param (string like `"Building/Complaint/NA/NA"`) + `openedDateFrom`/`openedDateTo`. Simple, no POST body needed.
2. **`records.search()`** — `POST /v4/search/records` with body containing `type` object (`{group, type, subType, category}`) + date range + `expand` for addresses/parcels/contacts/etc.

**Decision:** Use `records.search()` (POST) because it supports `expand` to pull addresses, parcels, contacts, owners, workflows in one call — same as the reference project. The search body `type` field maps to `recordTypeModel`: `{"group": "Building", "type": "Complaint", "subType": "NA", "category": "NA"}`.

The web select box key `"Building/Complaint/NA/NA"` maps to: group=Building, type=Complaint, subType=NA, category=NA.

## Data Volume Estimate

Building complaints since Jan 2020 = ~5 years × 12 months = 60 monthly chunks. At maybe 200-500 complaints/month, expect 12K-30K total records. Each record also fetches workflow history (separate API call per record). Conservative estimate: **2-4 hours for initial backfill** depending on API rate limits.

---

## Phase 1: Database Setup

- [x] **1.1** Create `application` schema in `who_owns_atl` DB
- [x] **1.2** Create `application.records` table (matches nbh_accela schema)
- [x] **1.3** Create indexes (search, permit_number, opened_date, last_action_date, geom GIST, description)
- [x] **1.4** Create helper views: `view_workflow_histories`, `view_contacts`, `view_addresses`, `view_parcels`
- [x] **1.5** Create `application.suffix_map` and `application.ordinal_map` tables
- SQL: `scripts/sql/01_create_application_schema.sql`

## Phase 2: GIS Data for Geometry Matching

We need `Address_Point` loaded into a `gis` schema for the geometry trigger. We already have the file (`data/json/geojson/latest/Address_Point.json`, 387MB). We also have `Tax_Parcel.json` (Atlanta city) — this is the one used in nbh_accela for parcel geometry fallback.

- [x] **2.1** Create `gis` schema (in 01_create_application_schema.sql)
- [x] **2.2** Load `Address_Point.json` → `gis."Address_Point"` (262K points via geopandas)
- [x] **2.3** Load `Tax_Parcel.json` → `gis."Tax_Parcel"` (171K parcels via geopandas)
- [x] **2.4** Create GIST + BTREE indexes on geometry and key columns
- [x] **2.5** Create geometry-matching trigger (`trg_update_record_geom`) — two-stage: address→parcel fallback
- Scripts: `scripts/06a_load_gis_for_accela.py`, `scripts/sql/02_create_geom_trigger.sql`

**Note:** The city Tax_Parcel is required — it has `LOWPARCELID` which maps Accela's parcel numbers to county parcel IDs. At minimum Fulton needs this bridge table. We also already have `fulton_parcels` and `dekalb_parcels` with geometry in `public` schema for the ownership data.

## Phase 3: Accela Pull Script

- [x] **3.1** Add dependencies to `pyproject.toml`: `accela` (git source), `accelapy>=0.3.5`, `python-dotenv`
- [x] **3.2** Create `scripts/06_pull_accela_records.py` — all features implemented:
  - Configurable `--type` (default: `Building/Complaint/NA/NA`)
  - `--from-date` / `--to-date` with auto monthly chunking
  - `--mode opened|updated|both`
  - Pagination (100/page), workflow history fetch, upsert, token refresh
  - Progress logging per-month/per-page, resume-friendly (idempotent)
- [x] **3.3** `last_action_date` + `last_action_info` calculation ported from nbh_accela
- **Smoke test:** Jan 2024 = 161 records, 99.4% geometry match rate
- **Full backfill running:** 2020-01-01 to 2026-02-18 (in progress)

## Phase 4: Post-Processing / Enrichment

- [x] **4.1** Geometry trigger verified: 99.3% match rate
- [x] **4.2** Status categorization: `closedDate` not available from search API; resolution determined from workflow status values (see Status Notes below)
- [x] **4.3** Created views in `scripts/sql/03_create_record_views.sql`:
  - `view_records_with_parcels` — records + Tax_Parcel bridge + `status_category` (Active/Resolved)
  - `view_records_fulton` — records joined to fulton_parcels (ownership flags) + `status_category`
  - `view_complaint_counts` — per-parcel stats: `resolved_complaints`, `active_complaints`, date range

## Phase 5: Incremental Updates Script

- [x] **5.1** Same script with `--mode updated` handles incremental updates
- [x] **5.2** Usage documented in script header and below:
  - **Initial backfill:** `uv run python scripts/06_pull_accela_records.py` (defaults: 2020-01-01 to today)
  - **Daily refresh:** `uv run python scripts/06_pull_accela_records.py --mode updated --from-date <yesterday> --to-date <today>`
  - **Other record types:** `uv run python scripts/06_pull_accela_records.py --type "Code/Complaint/NA/NA"`
  - Re-running is safe (idempotent upsert)

## Phase 6: Verification & Documentation

- [x] **6.1** Record count: **10,793 records** (74 months, ~2 hours)
- [x] **6.2** Geometry match: 99.3% (10,715/10,793). Tax_Parcel linkage: 95.5% (10,303/10,793)
- [x] **6.3** Updated `planning/02_project_status.md`
- [x] **6.4** Final stats below

**Backfill completed:** 2026-02-19 01:23 (~2 hours for 74 monthly chunks)

### Final Stats
| Metric | Value |
|---|---|
| Total records | 10,793 |
| With geometry | 10,715 (99.3%) |
| Linked to Tax_Parcel | 10,303 (95.5%) |
| Distinct parcels | 8,109 |
| Date range | 2020-01-02 to 2026-02-18 |
| Active complaints | 3,681 |
| Resolved complaints | 7,112 |

### Status Notes
- `closedDate` is NOT returned by the Accela search API
- Closure is determined by the workflow `status` field values
- "Resolved" = Closed, Complied, No Violation Found, Void, Complied - Dismissed, Judgement-Complied, Court Complied, Not Complied-Dismissed, Dismissed-Not Complied, Closed - Final-UTGE, Potential Duplicate
- "Active" = everything else (Assigned to Inspector, Stop Work Posted, In Review, Citation Served, etc.)
- 36 distinct status values total

---

## Key Decisions & Notes

- **Schema:** `application` (same as nbh_accela) — keeps permit data separate from ownership tables in `public`
- **GIS data:** Need to load Address_Point + Tax_Parcel into `gis` schema for the geometry trigger. This is a one-time load (~700MB of JSON).
- **Record type format:** The slash-separated string `"Building/Complaint/NA/NA"` maps to `recordTypeModel` fields: `{group: "Building", type: "Complaint", subType: "NA", category: "NA"}`
- **API endpoints used:**
  - `POST /v4/search/records` — main record search (type + date + expand)
  - `GET /v4/records/{id}/workflowTaskHistories` — workflow history per record
- **`accela` package source:** `https://github.com/jessedp/accela.git` — your fork, installed via `uv` git source
- **Parcel linkage:** Accela records contain `parcels[].parcelNumber` in raw_data → matches `gis."Tax_Parcel".LOWPARCELID` → bridges to county parcels via PARCELID.
- **Rate limits:** Accela API has rate limits (visible in response headers). Monthly chunking + pagination keeps requests reasonable. If we hit limits, add a sleep between pages.

## Resolved Questions

1. **POST for search** — use `records.search()` (POST /v4/search/records) like `pull_all_ranges()` in nbh_accela. Supports `expand` for full data in one call.
2. **City Tax_Parcel required** — need it loaded into `gis` schema. At least for Fulton, it bridges Accela parcel IDs → county LOWPARCELID.
3. **Building Complaint only for now** — type is configurable in the script, additional types added to the same table later.
