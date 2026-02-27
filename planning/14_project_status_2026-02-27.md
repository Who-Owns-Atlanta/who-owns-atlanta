# Project Status — 2026-02-27

Pipeline re-run completed this date, 15:30pm. A second run was completed later the same day
after implementing the `compute_stem()` improvement (see §3). All figures reflect that final state.

---

## 1. Pipeline State

Scripts are up to date and the DB is current. Two full pipeline runs were completed today:
the first after bringing the DB in sync with `a89e45f`'s `ADDRESS_STREET_BLOCKLIST` fix;
the second after implementing the `compute_stem()` improvement described in §3.

`validate_pipeline.py` result: **all checks pass, 1 known warning** (IH series in 9
clusters — structural limit of `STREET_ENTITY_LIMIT` gating, not a regression).

### Overall cluster distribution

| Band | Count |
|------|-------|
| Singletons (1 parcel) | 429,962 |
| Tiny (2–9) | 36,332 |
| Small (10–99) | 1,156 |
| Medium (100–499) | 55 |
| Large (500–1,999) | 12 |
| Mega (≥2,000) | 2 |
| **Total clusters** | **467,494** |
| Largest cluster | 3,333 |

---

## 2. Known Firm Benchmarks (post-run)

| Firm | Cluster | Parcels | Clusters | Status |
|------|---------|---------|----------|--------|
| Invitation Homes (IH/SFR XII/STAR/TBR/IH3/THR) | 8 | **2,832** (series total) | 9 fragments | WARN — address gate fragmentation (see §3) |
| Amherst (BAF ASSETS / ALTO ASSET / HOME SFR / CPI AMHERST / RH PARTNERS) | 3 | **2,490** | 1 | ✓ fused correctly |
| Pretium / FYR SFR BORROWER | 126 | **541** | 1 | ✓ separate from Amherst |
| FirstKey Homes (FKH SFR) | 20 | **1,224** | 1 | ✓ |
| Progress Residential | 468335 | **560** | 1 | ✓ correctly fissioned from cluster 77 |
| Home Partners of America (HPA) | 15 | **553** | 1 | ✓ fused (was fragmented before) |
| Tricon / TAH / SFR JV | 89 | **810** (cluster 89); **1,452** total | 40 | WARN — address gate fragmentation (see §3) |

Note on Amherst: the planning/13 figure of "492 parcels" was the pre-fusion size of
cluster 3 alone. 10b Pass A correctly fuses ~8 additional Amherst sub-clusters each run,
bringing the full family to ~2,490. Expected and correct.

Note on Progress Residential: previously in cluster 77, which also contained unrelated
FYR SFR TRS / HOME TRS entities. The new `BORROWER`-stripping in `compute_stem()` gave
Progress entities a clean `PROGRESS RESIDENTIAL` stem with high internal cohesion, causing
Pass B fission to correctly split them off into their own cluster (468335, 560 parcels).
Cluster 77 now holds the residual FYR SFR TRS / SFR INVESTMENTS entities (158 parcels).

### 10b Pass B fissions in final run

- **Cluster 77:** Progress Residential (560 parcels) split off → cluster 468335. Cluster 77
  retains FYR SFR TRS / HOME TRS / SFR INVESTMENTS (158 parcels — unrelated to Progress)
- **Cluster 126 (Pretium):** TRUE NORTH PROPERTY OWNER series split off →
  cluster 468334, 143 parcels (separate operator, not Pretium)

---

## 3. Tricon Fragmentation — Partially Resolved

### `compute_stem()` improvement (implemented 2026-02-27)

`BORROWER`, `PROPERTY`, `PROPERTIES`, `OWNER`, and `OWNERCO` were added to
`SUFFIX_NOISE` in `10b_cluster_refinement.py`, and the stripping logic was extended to
remove interior 4-digit year + optional 1–2 digit sequence tokens in addition to leading ones.

Effect on Tricon: `TRICON SFR 2024 3 BORROWER LLC` and `TRICON SFR 2020 2 BORROWER LLC`
now both stem to `TRICON SFR`. Pass A fused 5 additional clusters into cluster 89,
growing it from 540 → **810 parcels**. Fragment count reduced from 50 → **40 clusters**.

Simulation over 33K SOS-matched entities confirmed **zero false merges** introduced by
this change. The 10 new fusions triggered were all correct, including: CPI AMHERST SFR
PROGRAM (Amherst variant), HPA BORROWER ML (Home Partners series), BTR SCATTERED SITE
(comma/punctuation split), and small duplicate-owner pairs.

Incidental correct improvement: Progress Residential now stems cleanly to
`PROGRESS RESIDENTIAL`, enabling Pass B to fission it out of the mixed cluster 77.

### Remaining fragmentation

All Tricon/TAH/SFR JV entities mail to streets exceeding `STREET_ENTITY_LIMIT=30`:
- `1508 BROOKHOLLOW DR, SANTA ANA CA` — 35 entities
- `15771 RED HILL AVE, TUSTIN CA` — 85 entities

The stem fix unified `TRICON SFR *` variants but `TRICON SFR`, `TAH`, and `SFR JV`
are still three different stems. Bridging across families requires either:

1. **Known-alias list** — explicitly declare `{TRICON SFR, TAH, SFR JV}` as the same
   firm. Effective but requires manual curation for each new firm.
2. **Address-level corporate whitelist** — allow specific verified HQ addresses to bypass
   `STREET_ENTITY_LIMIT`. Inverse of `ADDRESS_STREET_BLOCKLIST`. Same curation burden.
3. **Accept remaining fragmentation** — 1,452 total Tricon parcels are present and
   searchable by name. Leaderboard rank is affected; data completeness is not.

---

## 4. Unnamed Top Clusters — Manual Research Needed

The following clusters with 100+ parcels have no confirmed umbrella identity:

| Cluster | Parcels | Sample names | Lead for research |
|---------|---------|--------------|-------------------|
| 1 | 1,699 | CBPIC GA OWNER I LLC, PARKWOOD LIVING LLC, PAGAYA SMARTRESI F1 FUND | Mixed — contains Pagaya AI-managed SFR fund + others; CBPIC = opaque; search EDGAR |
| 2 | 1,214 | CANOPY WEST LLC, CANOPY DEVELOPMENT GROUP LLC, STRYANT HOMES | Local Atlanta operator; search GA SOS |
| 6 | 608 | TROY STREET HOLDINGS 241 LLC, BROCK BUILT HOMES LLC | Brock Built = homebuilder; mixed cluster? |
| 4 | 566 | CORDIA GEORGIA 2 LLC, RAFFLES CV LLC, OLJ VENTURES LLC | Unknown; search GA SOS / OpenCorporates |
| 29 | 555 | RS RENTAL I LLC, AO PROPCO 1 LLC | Unknown; possibly Amherst-adjacent (Austin TX address?) |
| 15 | 553 | HPA CL2 LLC, HPA US1 LLC, SFR ACQUISITIONS 1 LLC | HPA = Home Partners of America (Blackstone) — now fused by stem fix; confirm identity |
| 7 | 519 | LWH CAREY PARK LLC, 119 PHARR ROAD OWNER LLC, SINOCOIN RE LLC | Unknown umbrella; search GA SOS |
| 34 | 452 | DAVINCI GA LLC, GRETZKY GA LLC, TOLSTOY GA LLC | Artist-name LLC series — likely local operator |
| 37 | 436 | PFIN II F LLC, VINEBROOK HOMES BORROWER 1 LLC | Vinebrook Homes = known SFR fund (NexPoint); confirm cluster scope |
| 10 | 405 | 788 HIGH RISE LLC, HIGHLAND PARK RESIDENCE LLC, MCKINLEY HOMES US LLC | Unknown; search GA SOS |
| 11 | 381 | LARKIN STREET HOMES LLC, DIVVY HOMES WAREHOUSE II LLC | Divvy Homes = known SFR operator (Brookfield) — confirm |

Recommended per-cluster process: search primary name on EDGAR full-text search,
then cross-reference in GA SOS portal. Most top-20 clusters can be resolved in
under an hour of manual research.

---

## 5. Validation Infrastructure

`scripts/validate_pipeline.py` is now the post-pipeline gate. It runs after 10b
and before the materialized view SQL rebuild.

**Current assertions:**
- Structural health: total clusters, total parcels, largest cluster ceiling (5,000), no mega (>10,000)
- Firm benchmarks: IH ≥2,500 parcels, Progress ≥500, Amherst ≥300, Pretium separate from Amherst, FirstKey ≥500
- Blocklist effectiveness: HOME SFR BORROWER and FYR SFR BORROWER in different clusters
- Institutional isolation: no institutional entities in large SOS-matched clusters
- Script consistency: `ADDRESS_STREET_BLOCKLIST` identical in scripts 04 and 10

Exits 0 (warnings allowed), exits 1 on hard failure. See planning/06_production_runbook.md
for placement in the full rebuild sequence.

---

## 6. What a "Next Data Refresh" Looks Like

When new county parcel exports arrive the minimal safe sequence is:

```bash
uv run scripts/01_load_parcels.py
uv run scripts/02_flag_corporate_owners.py
uv run scripts/03_normalize_addresses.py
uv run scripts/04_ownership_network.py
uv run scripts/09_enrich_owners_sos.py      # must run before 10 (adds SOS columns)
uv run scripts/10_sos_network_enrichment.py
uv run scripts/10b_cluster_refinement.py
uv run scripts/validate_pipeline.py         # gate — fix any failures before continuing
PGPASSWORD=woa psql -h localhost -p 5434 -U woa who_owns_atl \
  -f scripts/sql/04_create_materialized_views.sql
uv run scripts/build_static_pages.py
bash scripts/build_tiles.sh
```

Scripts 08 (SOS fuzzy matching) only needs to re-run if new corporate owner names
appear in the parcel data that weren't matched before, or if the SOS bulk download
is refreshed. It is expensive (~2 hours) and can be skipped on routine county refreshes.
