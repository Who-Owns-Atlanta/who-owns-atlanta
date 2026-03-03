# Production Runbook — Who Owns Atlanta?

**Created:** 2026-02-20
**Updated:** 2026-02-27

This runbook covers all recurring operational tasks: data refreshes, pipeline
changes, and frontend deployment. Each section is self-contained — find your
update type and follow only those steps.

---

## Quick Reference — What to Run for What

| Trigger | Sections to follow |
|---|---|
| New county parcel export (Fulton or DeKalb) | [A. County Parcel Data](#a-update-county-parcel-data) |
| New GA SOS bulk download | [B. SOS Data](#b-update-sos-data) |
| City GIS layer changed (neighborhoods, NPU, council) | [C. City GIS Data](#c-update-city-gis-data) |
| Pull new building complaints from Accela | [D. City Permits (Accela)](#d-update-city-permits-accela) |
| Tuning clustering logic / blocklists / thresholds | [E. Pipeline Changes](#e-pipeline-changes-clustering-refinement) |
| HTML/CSS/JS change only | `uv run scripts/build_static_pages.py` only |
| API code change | Restart `woa_api` Docker container only |
| nginx config change | `nginx -s reload` only |

---

## Data Sources

| Data | Source | Portal | Typical cadence |
|---|---|---|---|
| Fulton County parcels | Fulton County GIS | [fultoncountyga.gov](https://gisdata.fultoncountyga.gov/datasets/fulcogis::tax-parcels/about) | Annual (Jan) |
| DeKalb County parcels | DeKalb County GIS | [dekalbinsights](https://dekalbinsights-dekalbgis.opendata.arcgis.com/datasets/731f52734f2346d6b939ff7337fcfaa2_0/) | Annual (Dec) |
| GA SOS entities | GA Secretary of State | [ecorp.sos.ga.gov](https://ecorp.sos.ga.gov/) bulk download | As needed |
| City GIS (neighborhoods, NPU, council) | City of Atlanta Open Data | [dcp-coaplangis.opendata.arcgis.com](https://dcp-coaplangis.opendata.arcgis.com/) | Infrequent |
| City permits (Accela) | City of Atlanta Accela | API (no portal download) | Monthly |

Downloaded files land in `data/json/geojson/latest/` (gitignored, ~1.7GB total).
After any data refresh, update `web/frontend/data/datasources.json` with the new
`admin_date` and `last_loaded` dates — the site's FAQ data-sources table reads from it.

---

## Pipeline Map

Scripts run in numbered order. Not every update requires all of them.

```
01_load_parcels.py          — Load Fulton + DeKalb GeoJSON → PostGIS; create parcels_unified view
02_flag_corporate_owners.py — Flag is_corporate / is_institutional on parcel rows
03_normalize_addresses.py   — Normalize owner mailing addresses via libpostal
04_ownership_network.py     — Build owner_entities + ownership_clusters (address/name graph)
                              ⚠ Drops and recreates owner_entities from scratch
    [06a_load_gis_for_accela.py] — Load Atlanta GIS layers (Address_Point, Tax_Parcel) for permit matching
    [06_pull_accela_records.py]  — Pull building complaints from Accela API (independent of parcel pipeline)
07_load_sos.py              — Load GA SOS bulk TSV files into sos.* schema (~4.3M entities, ~49M officers)
08_match_sos.py             — Fuzzy-match parcel corporate owner names → SOS entities   ⏱ ~90 min
09_enrich_owners_sos.py     — Write SOS fields (RA, status, etc.) back to owner_entities
                              ⚠ Must run after 04 (which drops owner_entities) and before 10
10_sos_network_enrichment.py — Add RA/officer/address edges from SOS; re-cluster
                              ⚠ Drops ownership_clusters CASCADE (takes mv_leaderboard with it)
10b_cluster_refinement.py   — Pass A: fuse fragmented series; Pass B: fission false bridges
validate_pipeline.py        — Assert known-firm benchmarks and structural invariants ← run here
12_portfolio_demographics.py — Calculate neighborhood demographic profiles for clusters
capture_maps_playwright.py   — Generate income/renter map images for top owners
    [scripts/sql/04_create_materialized_views.sql] — Recreate mv_address_search, mv_parcel_permits,
                                                     mv_cluster_stats, mv_leaderboard
11_city_enrichment.py       — Spatial-join Atlanta GIS → add city_neighborhood/npu/council_district
                              to fulton_parcels and dekalb_parcels rows
build_static_pages.py       — Generate all HTML (owner profiles, leaderboards, geo pages)
build_tiles.sh              — Run tippecanoe → vector tiles in /var/www/who-owns-atlanta/tiles/
```

---

## A. Update County Parcel Data

**When:** New Fulton or DeKalb GeoJSON export is available (typically annually).

**Effect:** Ownership network is rebuilt from scratch. All downstream outputs regenerate.

**Time:** ~30–45 min for scripts 01–04+10; ~90 min if script 08 also runs (see note).

### Step 1 — Download new GeoJSON

Replace the relevant file in `data/json/geojson/latest/`:
- Fulton: `Fulton_County_Tax_Parcel.json` (~457MB) from [fultoncountyga.gov](https://gisdata.fultoncountyga.gov/datasets/fulcogis::tax-parcels/about)
- DeKalb: `Dekalb_County_Tax_Parcels.geojson` (~451MB) from [dekalbinsights](https://dekalbinsights-dekalbgis.opendata.arcgis.com/datasets/731f52734f2346d6b939ff7337fcfaa2_0/)

### Step 2 — Run the parcel pipeline

```bash
# Load GeoJSON → PostGIS (drops and recreates fulton_parcels / dekalb_parcels)
uv run scripts/01_load_parcels.py

# Flag corporate / institutional owners
uv run scripts/02_flag_corporate_owners.py

# Normalize owner mailing addresses via libpostal
# (libpostal Docker container must be running on port 6789)
uv run scripts/03_normalize_addresses.py

# Build ownership graph (drops and recreates owner_entities + ownership_clusters)
uv run scripts/04_ownership_network.py
```

### Step 3 — Re-enrich with SOS (required after script 04)

Script 04 drops `owner_entities` and recreates it bare. Script 09 must re-add the
SOS columns before script 10 can read them.

```bash
# Write SOS fields back to the freshly rebuilt owner_entities
uv run scripts/09_enrich_owners_sos.py

# Add RA / officer / SOS-address edges; re-cluster
# ⚠ This drops ownership_clusters CASCADE (mv_leaderboard also gone)
uv run scripts/10_sos_network_enrichment.py
```

> **Skip script 08** unless new corporate owner names appeared that weren't in the
> previous county export. Script 09 re-uses the existing `sos_matches` table, which
> is preserved across pipeline runs. If you do need to re-run 08, expect ~90 minutes.

### Step 4 — Post-cluster refinement and validation

```bash
uv run scripts/10b_cluster_refinement.py

# Must pass before continuing. Exits 1 on hard failure.
uv run scripts/validate_pipeline.py
```

### Step 5 — Recreate materialized views

```bash
PGPASSWORD=woa psql -h localhost -p 5434 -U woa who_owns_atl \
  -f scripts/sql/04_create_materialized_views.sql
```

Creates (in dependency order): `mv_address_search` → `mv_parcel_permits` →
`mv_cluster_stats` → `mv_leaderboard`. Safe to re-run at any time.

### Step 6 — City enrichment (neighborhood / NPU / council district)

```bash
uv run scripts/11_city_enrichment.py
```

Spatial-joins Atlanta GIS boundaries into `city_neighborhood`, `city_npu`,
`city_council_district` columns on parcel rows. Requires GIS layers to already
be loaded (they persist in PostGIS; re-run [section C](#c-update-city-gis-data)
only if the boundary files changed).

### Step 6b — Calculate demographics

```bash
uv run scripts/12_portfolio_demographics.py
```

### Step 6c — Generate owner map images

```bash
# Uses Playwright + xvfb-run to capture demographic visuals for portfolios
# (Requires xvfb and playwright chromium)
xvfb-run uv run scripts/capture_maps_playwright.py --workers 4
```

### Step 7 — Rebuild outputs


```bash
uv run scripts/12_portfolio_demographics.py
```

### Step 7 — Rebuild outputs

```bash
uv run scripts/build_static_pages.py
bash scripts/build_tiles.sh
```

### Step 8 — Update datasources.json

Edit `web/frontend/data/datasources.json` — update `admin_date` (from the GIS
portal's "Last Modified" equivalent timestamp) and `last_loaded` (today) for the affected
county key (`fulton_parcels` or `dekalb_parcels`).

---

## B. Update SOS Data

**When:** New GA SOS bulk download is obtained (as of 2026-02: weekly download [$5000/yr], one-time [$500] ), or matching quality has noticeably degraded.

**Effect:** Refreshes entity status, registered agents, officers. Network graph re-runs.

**Time:** Script 07 ~20 min; script 08 ~90 min; scripts 09–10b ~10 min.

GA SOS bulk download: [ecorp.sos.ga.gov](https://ecorp.sos.ga.gov/) → Corporations →
Bulk Data. Download the ZIP; extract the five TSV files into `data/sos/`.

Expected files:

| File | Rows | Loaded into |
|---|---|---|
| `BizEntity.txt` | ~4.3M | `sos.entities` |
| `BizEntityAddress.txt` | ~4.7M | `sos.addresses` |
| `BizEntityOfficers.txt` | ~49M | `sos.officers` |
| `BizEntityRegisteredAgents.txt` | ~10.3M (latin-1) | `sos.registered_agents` |

*(Skip `BizEntityFilingHistory.txt` and `BizEntityStock.txt` — not used.)*

```bash
# Drop and reload sos.* schema from TSV files
uv run scripts/07_load_sos.py

# Re-match parcel corporate owner names → SOS entities
# ⏱ ~90 min; uses 12 cores by default
uv run scripts/08_match_sos.py

# Then continue from section A, Step 3 (script 09 onward)
uv run scripts/09_enrich_owners_sos.py
uv run scripts/10_sos_network_enrichment.py
uv run scripts/10b_cluster_refinement.py
uv run scripts/validate_pipeline.py

PGPASSWORD=woa psql -h localhost -p 5434 -U woa who_owns_atl \
  -f scripts/sql/04_create_materialized_views.sql

uv run scripts/build_static_pages.py
# No tile rebuild needed — tile content (parcel geometry, is_corporate) doesn't change
```

Update `ga_sos` in `web/frontend/data/datasources.json` with new dates.

---

## C. Update City GIS Data

**When:** 
  - Tax Parcel : yearly, June-ish
  - Address Point: any time, supposedly constantly updated
  - Neighborhood boundaries, NPU assignments, or council district line changes go in effect (census, adminstrative splits/consolidations, etc.)

**Effect:** Updates `city_neighborhood`, `city_npu`, `city_council_district` columns on parcel rows, and refreshes the `mv_address_search` materialized view.

**Time:** ~10 min.

Download updated GeoJSON from [dcp-coaplangis.opendata.arcgis.com](https://dcp-coaplangis.opendata.arcgis.com/)
and replace files in `data/json/geojson/latest/`:
- `Address_Point.json`
- `Tax_Parcel.json`
- `Neighborhood.json`
- `NPU.json`
- `Official_City_Council_Districts.geojson`



```bash
# Reload Address_Point and Tax_Parcel into gis.* schema
uv run scripts/06a_load_gis_for_accela.py

# Re-run spatial join → updates city_* columns on parcel rows
uv run scripts/11_city_enrichment.py

# Refresh address search view (reads city_neighborhood)
PGPASSWORD=woa psql -h localhost -p 5434 -U woa who_owns_atl \
  -c "REFRESH MATERIALIZED VIEW mv_address_search;"

# Regenerate static pages (owner profiles show neighborhood/NPU/council)
uv run scripts/build_static_pages.py
# No tile rebuild needed — tiles carry cluster/corporate flags, not city fields
```

Update the relevant key(s) in `web/frontend/data/datasources.json`
(`atlanta_gis_neighborhoods`, `atlanta_gis_npu`, `atlanta_gis_council`, `atlanta_address_point`).

---

## D. Update City Permits (Accela)

**When:** Pulling new building complaint records (monthly or on demand).

**Effect:** Updates `application.records`; no parcel pipeline re-run needed.

**Time:** ~5–15 min depending on date range.

```bash
# Pull new records for a date range
uv run scripts/06_pull_accela_records.py \
  --type "Building/Complaint/NA/NA" \
  --from-date 2026-02-01 \
  --to-date 2026-02-28

# Or pull all records updated since a given date (incremental)
uv run scripts/06_pull_accela_records.py --mode updated \
  --type "Building/Complaint/NA/NA" \
  --from-date 2026-02-01 --to-date 2026-02-28

# Refresh the permit materialized view
PGPASSWORD=woa psql -h localhost -p 5434 -U woa who_owns_atl \
  -c "REFRESH MATERIALIZED VIEW mv_parcel_permits;"

# Regenerate static pages (owner profiles show open/resolved permit counts)
uv run scripts/build_static_pages.py
```

No tile rebuild. No parcel pipeline. No schema changes.

Update `accela.last_loaded` in `web/frontend/data/datasources.json`.

---

## E. Pipeline Changes (Clustering Refinement)

**When:** Tuning thresholds, blocklists, SUFFIX_NOISE, or other clustering logic
without changing source parcel data.

This is the lightest rebuild path — scripts 01–03 and 07–08 do not need to re-run
because parcel data and SOS matching haven't changed.

```bash
# Re-run the ownership graph with updated logic
uv run scripts/04_ownership_network.py

# Re-add SOS columns (04 drops owner_entities bare each time)
uv run scripts/09_enrich_owners_sos.py

# Re-run SOS network enrichment
uv run scripts/10_sos_network_enrichment.py

# Fission/fusion post-processing
uv run scripts/10b_cluster_refinement.py

# Calculate demographics
uv run scripts/12_portfolio_demographics.py

# Generate owner map images
xvfb-run uv run scripts/capture_maps_playwright.py --workers 4

# Validate — fix any failures before proceeding
uv run scripts/validate_pipeline.py

# Recreate materialized views
PGPASSWORD=woa psql -h localhost -p 5434 -U woa who_owns_atl \
  -f scripts/sql/04_create_materialized_views.sql

# Rebuild outputs
uv run scripts/build_static_pages.py
bash scripts/build_tiles.sh
```

### validate_pipeline.py — what it checks

Run after `10b` and before the materialized view SQL. Exits 0 if all hard assertions
pass (warnings are allowed); exits 1 on failure.

| Check | Threshold |
|---|---|
| Total clusters | ≥ 400,000 |
| Total tracked parcels | ≥ 500,000 |
| Largest cluster | ≤ 5,000 |
| No cluster | > 10,000 |
| Invitation Homes parcels (IH/SFR XII/STAR/TBR series) | ≥ 2,500 |
| IH series cluster count | ≤ 3 (WARN only — known address-gate fragmentation) |
| Progress Residential parcels | ≥ 500 |
| Amherst (BAF ASSETS/ALTO/HOME SFR/CPI AMHERST) | ≥ 300 |
| Pretium (FYR SFR) and Amherst in separate clusters | must differ |
| HOME SFR BORROWER and FYR SFR BORROWER in separate clusters | must differ |
| Institutional entities in large SOS-matched clusters | == 0 |
| `ADDRESS_STREET_BLOCKLIST` identical in scripts 04 and 10 | must match |

### Key tuning knobs

| What | Where | Notes |
|---|---|---|
| Street-level address hub gate | `STREET_ENTITY_LIMIT = 30` in `10_sos_network_enrichment.py` | Lower = fewer merges; Tricon split at ≤30 because Tustin CA has 85 entities |
| Suffix noise stripping | `SUFFIX_NOISE` in `10b_cluster_refinement.py` | Controls what `compute_stem()` strips; currently includes BORROWER/OWNER/PROPERTY |
| Series fusion cap | `MAX_MERGE_PARCELS = 5000` in `10b_cluster_refinement.py` | Prevents fusion of legitimately distinct large clusters |
| Fission minimum size | `FISSION_THRESHOLD = 300` in `10b_cluster_refinement.py` | Only clusters this big are examined for false bridges |
| Address bridge blocklist | `ADDRESS_STREET_BLOCKLIST` in both `04` and `10` | Must be kept identical in both files; validate_pipeline checks this |
| Commercial RA skip list | `COMMERCIAL_RA_SKIP` in `10_sos_network_enrichment.py` | National/professional RAs excluded from creating cluster edges |
| Global officer filter | `organizer/incorporator` with `global_sos_count > 500` in `10` | Prevents filing-service officers from acting as bridges |
| SOS merge size gate | `MAX_MERGE_PARCELS = 200` (Pass 2 in `10`) | SOS edges only between clusters each ≤200 parcels |

---

## Materialized View Reference

| View | Built from | Refresh trigger | Notes |
|---|---|---|---|
| `mv_address_search` | `gis."Address_Point"` | After city GIS reload | Typeahead search |
| `mv_parcel_permits` | `application.records` | After any permit pull | Can `REFRESH` in place |
| `mv_cluster_stats` | `ownership_clusters` + parcels | After clustering re-run | Dropped by script 10 CASCADE — must recreate, not refresh |
| `mv_leaderboard` | `mv_cluster_stats` | After clustering re-run | Dropped by script 10 CASCADE — must recreate, not refresh |

`scripts/sql/04_create_materialized_views.sql` recreates all four in dependency order.
Safe to re-run at any time — it drops before recreating.

> **Why CASCADE?** `scripts/10_sos_network_enrichment.py` runs
> `DROP TABLE ownership_clusters CASCADE`, which cascades to `mv_cluster_stats` and
> `mv_leaderboard`. After any clustering run, those views do not exist and must be
> recreated (not `REFRESH`ed).

---

## Frontend Production Deployment

Before deploying to production:

1. **Set production tile URL** — update `PROD_TILES_URL` in `web/frontend/js/app.js`
   with the live CloudFront distribution URL.
2. **Verify hostname logic** — production domain must not be in the `DEV_HOSTNAMES`
   array in `app.js`.
3. **API CORS** — FastAPI `woa_api` currently allows all origins (`*`). Restrict to
   `who-owns-atlanta.org` for production.
