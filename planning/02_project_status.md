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
- [ ] libpostal for address normalization
- [ ] Ownership network/graph logic
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
- **Fulton County:** 370,189 parcels (63,171 corporate = 17.1%)
- **DeKalb County:** 245,766 parcels (35,596 corporate = 14.5%)
- **Total:** 615,955 parcels in `parcels_unified` view

## Next steps
1. Address normalization with libpostal
2. Ownership network clustering
3. Tests
