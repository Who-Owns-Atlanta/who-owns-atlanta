# DEVELOPMENT ENVIRONMENT GUIDELINES

- **IMPORTANT** You are following plans in ./planning/ , updating as you progress as well as writing new ones there.

- **IMPORTANT** This file should be AGENTS.md . IF `CLAUDE.md` or `GEMINI.md` exist, they should be symlinks to this file. DO NOT OVERWRITE THE SYMLINKS.

- **IMPORTANT** After significant changes, make sure to ASK about commiting and updating this file as necessary.

- `python` scripting and web environment managed with `uv`, packaged under `docker` for production

# CREDENTIALS
- The `@.env` contains all credentials - database, APIs
- prefix PGPASSWORD= to all psql cli commands
- DB: `postgresql://woa:woa@localhost:5434/who_owns_atl`  (Docker PostGIS, port 5434)

# DATA SOURCES
- `web/frontend/data/datasources.json` is the **single source of truth** for all input file paths and provenance.
- Pipeline scripts load it via:
  ```python
  import json
  from pathlib import Path
  def _load_sources():
      root = Path(__file__).resolve().parent.parent
      return json.load(open(root / "web/frontend/data/datasources.json"))
  SOURCES = _load_sources()
  ```
- GeoJSON source files live in **dated subdirs**: `data/json/geojson/YYYY-MM-DD/` — there is no `latest/`.
- `data/json` is a symlink to `/home/jesse/projects/data/gis_json/` (shared with another project).
- SOS bulk files: `data/text/ga_sos/YYYY-MM-DD/` (currently `2026-02-18/`).

# DB SCHEMA FACTS
- `is_corporate`, `is_institutional` — columns on **`fulton_parcels`** and **`dekalb_parcels`**, NOT on `owner_entities`.
- `owner_names` — `text[]` array on `ownership_clusters` and `mv_leaderboard`; use `owner_names[1]` for primary name.
- SOS match — `owner_entities` has no `sos_match_count`; use `sos_control_number IS NOT NULL`.
- `parcel_count` on `ownership_clusters` / `mv_leaderboard` is `numeric`, cast to `int` as needed.

# RELEASES & ARCHIVES
- Release dumps: `dumps/vYYYYMMx.N.dump` (local only, not in git). Manifest: `dumps/MANIFEST.md`.
- Archive a release into a named DB: `scripts/db_archive.sh v202603A.1` → creates `woa_v202603a1`.
  - Requires tiger/tiger_data/topology schemas + extensions pre-created (the script handles this).
- Cross-release comparison: `uv run scripts/compare_releases.py who_owns_atl woa_v202603a1`
- `who_owns_atl` = always the live working DB; pipeline scripts DROP/recreate tables, no manual reset needed.

# TESTING/VALIDATION LOCATIONS
- web:  http://who-owns-atlanta.local/

# TOOLS
Don't ask permission before running read-only or non-destructive commands. If a command only reads/lists/searches - NO write, delete, move, or change - run it immediately without narrating your intent first.

**

Most commong linux tools exist; use any tools liberally. These tools are extrememly releve to this project:
- `uv` - this is a managed `Python` project.
- `curl` - check the api or website yourself!
- `git` - this is under source control with `git`.
- `psql` - check your postgres/gis queries!
    - you MUST prefix PGPASSWORD=  for psql cli commands to succeed
- `rg`, grep  (ripgrep)
- web:
  - `playwright` skill or mcp
  - `shot-scraper` - cli tool to take screeshots of web pages so you can "view" your changes.  [local, --help docs](docs/shot-scraper_help.txt) , [fuller, remote docs](https://shot-scraper.datasette.io/en/stable/screenshots.html). DO NOT PREVIEW EMAIL HTML


