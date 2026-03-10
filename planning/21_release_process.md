# Release Process — Who Owns Atlanta

## Versioning Scheme: Calendar Versioning

**Format: `YYYYMMx.N`**
- `YYYYMM` = year + month of data vintage
- `x` = data release letter within that month (A = first pipeline run, B = second, etc.)
- `.N` = code/interface increment within a data release (1, 2, 3...)

**Examples:**
- `202603A.1` = March 2026, first data release, initial code version ← **current**
- `202603A.2` = code-only fix, same March data
- `202603B.1` = second pipeline run in March (new data vintage)
- `202604A.1` = first April data release

**Why not SemVer?** Data freshness is the primary signal of "change" for users. CalVer communicates the data vintage at a glance. No backwards-compatibility concept applies here.

---

## Two Release Types

### Type A: Code Release (interface/API only)
- Does NOT re-run pipeline or rebuild tiles/static pages
- Increments `.N` only (e.g., `202603A.1` → `202603A.2`)
- Deploy via `deploy.sh` rsync only

### Type B: Data Release (pipeline re-run)
- Advances the letter (e.g., `202603A.x` → `202603B.1`) or month if applicable
- Full pipeline run, new db dump, rebuild tiles + static pages
- Archive the previous dump entry in `dumps/MANIFEST.md` before overwriting

---

## Branching Strategy: Tags, Optional Branches

**Primary mechanism: git tags**
- Tag every release after the deploy commit
- Tags are lightweight, searchable, and easy to check out
- The db dump corresponding to a release is noted in `dumps/MANIFEST.md`

**Create a branch only when:**
- A bug must be fixed in a shipped release while `main` has breaking in-progress work (rare)
- For most fixes: commit to `main`, tag, deploy — no branch needed

---

## Release Notes Approach

**Two layers of documentation:**

1. **`CHANGELOG.md`** (repo root, linkable from the site):
   - Structured sections per release, newest first
   - Format: heading with version + date, 2–5 bullet points of notable changes, data stats for Type B releases

2. **Annotated git tag** message:
   - Same summary paragraph as the CHANGELOG section
   - Append `git log --oneline v<prev>..HEAD` to list commits
   - Visible via `git show v202603A.1` — no GitHub UI required

---

## Checklists

### Code Release Checklist (Type A — increment `.N`)
- [ ] Commit all code changes to `main`
- [ ] Run `shot-scraper` or manual test at http://who-owns-atlanta.local/ to verify
- [ ] Run `deploy.sh` (rsync code only — no tile/page rebuild)
- [ ] Smoke test production URL
- [ ] Add section to `CHANGELOG.md`, commit it
- [ ] `git tag -a vYYYYMMx.N` — message: summary + `git log --oneline v<prev>..HEAD`
- [ ] `git push origin vYYYYMMx.N`

### Data Release Checklist (Type B — advance letter)
- [ ] Run full pipeline: scripts 01 → 13 (see `06_production_runbook.md`)
- [ ] Run `validate_pipeline.py` — must pass all firm benchmarks
- [ ] Rebuild static pages: `build_static_pages.py`
- [ ] Rebuild vector tiles: `build_tiles.sh`
- [ ] Upload tiles to R2 (wrangler / rclone sync)
- [ ] Rsync static pages to production
- [ ] Deploy code: `deploy.sh`
- [ ] Smoke test production: spot-check known clusters, map tiles, search
- [ ] `pg_dump` the new data
- [ ] Update `dumps/MANIFEST.md` (version, date, row counts, pipeline notes)
- [ ] Update README dataset stats (parcel count, cluster count, etc.)
- [ ] Add section to `CHANGELOG.md`
- [ ] Commit everything (dump manifest, README, CHANGELOG)
- [ ] `git tag -a vYYYYMMx.1` — message: summary + `git log --oneline v<prev>..HEAD`
- [ ] `git push origin vYYYYMMx.1`

---

## Pending Feature Routing

| Feature | Type | Target release |
|---------|------|----------------|
| Fix individual over-merging (Pass A → corporate only) | **Type B** (pipeline re-run) | `v202603B.1` |
| Fix joint owner clustering (A & B = B & A) | **Type B** (pipeline re-run) | `v202603B.1` |
| Zoning/home-type filter | **TBD** — check if zoning codes already in parcel tiles | Type A if data in tiles; else Type B |

---

## Dump Storage

- Dumps excluded from git via `dumps/.gitignore`
- `dumps/MANIFEST.md` tracks each archived dump (version, date, parcel count, cluster count, notes)
- Current dump filename convention: `dumps/vYYYYMMx.N.dump` (stored locally, not in git)
