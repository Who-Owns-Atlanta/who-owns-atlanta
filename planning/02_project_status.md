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
- [ ] Python dependencies (geopandas, psycopg2, networkx, libpostal, etc.)
- [ ] PostgreSQL/PostGIS database (Docker)
- [ ] Data loading pipeline (GeoJSON → PostGIS)
- [ ] libpostal for address normalization
- [ ] Schema unification (Fulton + DeKalb → unified parcel table)
- [ ] Corporate owner name filtering
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

## Pipeline architecture — TBD
Two options, decide when we start building:
1. **Ordered scripts** (`01_load.py`, `02_normalize.py`, ...) — simple, fast to iterate
2. **CLI with subcommands** (`uv run who-owns-atl load`, `... normalize`) — cleaner long-term

## Next steps
1. Set up Docker PostGIS container
2. Install Python dependencies
3. Build data loading pipeline (GeoJSON → unified PostGIS table)
4. Implement corporate owner name filtering
5. Address normalization with libpostal
6. Ownership network clustering
