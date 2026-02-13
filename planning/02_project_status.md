# Project Status — 2026-02-12

## What's in place
- [x] Project directory structure
- [x] Git repo initialized
- [x] `uv` project configured (Python 3.12, `pyproject.toml`)
- [x] Two primary parcel datasets: Fulton County (457MB), DeKalb County (451MB)
- [x] Atlanta Tax_Parcel reserved for later enrichment (council/NPU/neighborhood linkage)
- [x] Reference overlays: city limits, neighborhoods, NPU, council districts, address points, zoning
- [x] Workflow reference docs (two LLM consultations + Horizontal Holdings PDF)
- [x] CLAUDE.md / AGENTS.md for AI assistant guidelines
- [x] Linked `tmp_nbh_accela` repo

## What's NOT in place yet
- [x] Python dependencies (geopandas, psycopg2-binary, sqlalchemy, geoalchemy2, networkx)
- [x] PostgreSQL/PostGIS database (Docker — `woa_postgis` on port 5434)
- [x] Data loading pipeline (GeoJSON → PostGIS: `scripts/01_load_parcels.py`)
- [x] Schema unification (Fulton + DeKalb → `parcels_unified` view)
- [x] Corporate owner name filtering (`scripts/02_flag_corporate_owners.py`, `is_corporate` column)
- [x] libpostal for address normalization (`scripts/03_normalize_addresses.py`, `addr_norm_lookup` table)
- [x] Ownership network/graph logic (`scripts/04_ownership_network.py`, `owner_entities` + `ownership_clusters` tables)
- [ ] GA Secretary of State scraper (deferred — exists in JS, migrate when needed)
- [ ] Tests

## Decisions made
- **Primary data:** Fulton County + DeKalb County only. Atlanta city data is redundant (counties cover it).
- **Database:** Docker PostGIS
- **Python:** 3.12 via `uv`
- **libpostal:** Will install as a dependency (not optional)
- **GA SOS scraper:** Deferred until pipeline needs it
- **Owner filtering:** Corporate/institutional owners identified by name pattern, not homestead exemption

## Corporate Owner Name Filter (starting point)
```regex
/\b(llc|inc|corp|ltd|lp|assoc|assn|foundation|company|system|board of regents|department of transportation|plan|pc|venture|ventures|invest|investments|partners)\b/i
```
This catches LLCs, corporations, limited partnerships, associations, foundations, government entities,
professional corporations, and investment groups. Will expand as we encounter more patterns in the data.

## Pipeline architecture
Ordered scripts (`scripts/01_load.py`, `scripts/02_flag_corporate.py`, ...) — simple, fast to iterate.

## Database stats
- **Fulton County:** 370,189 parcels (67,719 corporate = 18.3%, 22,620 institutional = 6.1%)
- **DeKalb County:** 245,766 parcels (37,093 corporate = 15.1%, 13,036 institutional = 5.3%)
- **Total:** 615,955 parcels in `parcels_unified` view
- **Owner entities:** 543,421 distinct (name, address, county) groups
- **Ownership clusters:** 471,679 total, 34,233 with multiple linked entities
- **Address normalization:** 510,849 distinct addresses normalized via libpostal

## Known issues / observations
- **PO Box collapse (fixed):** libpostal strips PO Box numbers, leaving just city/state/zip. This linked thousands of unrelated entities. Fixed by skipping city/zip-only addresses in graph construction.
- **Cluster 1 (~7.9K parcels, 1638 entities):** Linked via real commercial office addresses (270 Washington St = government center, 1100 Spring St = real estate offices, 345 Park Ave NYC, etc.). Arguably correct — many LLCs sharing office space is a real ownership signal.
- **Cluster 3 (990 parcels, 990 entities):** All subdivision/condo names used as owner names (BRANDYWINE, WILDWOOD PARK, OXFORD VILLAGE). Data quirk, not real ownership. Linked by shared empty address.
- **Typo catches working well:** "GEOGRIA POWER COMPANY" → "GEORGIA POWER CO" (cluster 112), "HABITA FOR HUMANITY" → "HABITAT FOR HUMANITY" (cluster 93), "PROMISE HOMES BORROWER I LLCC" → correct spelling (cluster 3403).

## Next steps
1. Investigate/refine mega-cluster (split or cap address linking)
2. GA Secretary of State scraper
3. Tests
