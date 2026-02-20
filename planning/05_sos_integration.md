# Plan: GA SOS Data Integration

**Created:** 2026-02-19
**Status:** In progress

---

## Data received

GA SOS bulk download — 6 TSV files (CRLF, ASCII/latin-1), loaded into `sos` schema:

| Table | Rows | Key columns |
|---|---|---|
| `sos.entities` | 4,297,864 | control_number, business_id, business_name, business_type_desc, entity_status, registered_agent_id, foreign_state |
| `sos.addresses` | 4,660,108 | business_id, control_number, street_address1-2, city, state, zip |
| `sos.officers` | 49,301,637 | control_number, description (role), first_name, last_name, company_name, line1-2, city, state, zip |
| `sos.registered_agents` | 10,275,158 | registered_agent_id, name, line1-4, city, state, zip |

Skipped for now: `BizEntityFilingHistory.txt` (30M rows), `BizEntityStock.txt` (2M rows).

### Data quirks (handled in load script)
- `control_number` not unique (~39 dupes — same entity, different NAICS sub-code)
- Some integer-valued columns contain literal string `"NULL"` (normalized to empty on load)
- Trailing empty fields often omitted (rows padded to full column count on load)
- Backslashes in field values escape the newline in COPY text format (escaped on load)
- `BizEntityRegisteredAgents.txt` is ISO-8859 (latin-1), rest are ASCII
- 323 rows in registered_agents have extra columns (truncated to 14 on load)
- All ID/count columns stored as TEXT due to mixed "NULL"/empty/integer data in source

### Indexes created
- `sos.entities`: control_number, business_id, registered_agent_id, entity_status
- `sos.entities`: GIN tsvector on business_name (full-text), GIN trigram on business_name (fuzzy)
- `sos.addresses`: business_id, control_number
- `sos.officers`: control_number, business_id
- `sos.registered_agents`: registered_agent_id

---

## Integration phases

### Phase 1: Name matching — parcel owners → SOS entities ✅ COMPLETE

Match ~45K distinct corporate owner names from counties against `sos.entities.business_name`.

**Script:** `scripts/08_match_sos.py`

**Strategy:**
1. **Normalization:** Enhanced punctuation stripping.
2. **Splitting:** Split names on `&`, `AND`, `ET AL` to handle multi-entity ownership.
3. **Hybrid Parallel Fuzzy Matching:** 12-core multi-processing with SQL GIN Trigram index + rapidfuzz re-scoring.

**Actual match results (2026-02-20):**

| Match type | Count | Notes |
|---|---|---|
| exact (1.0) | 13,090 | Normalized exact match |
| trgm_high ≥0.80 | 26,792 | High confidence — re-scored with rapidfuzz |
| trgm_low 0.65–0.79 | 2,989 | Low confidence — flag only, not used for enrichment |
| **Total** | **42,871** | **96.5% matched** |

---

### Phase 2: Enrich owner_entities with SOS data ✅ COMPLETE

Once matches are confirmed, propagate SOS fields back to `owner_entities`:

**Script:** `scripts/09_enrich_owners_sos.py`

**Results (2026-02-20):**
- 50,168 owner_entities enriched (exact + trgm_high only; trgm_low skipped)
- Top foreign state: Delaware (5,335)
- Join uses `normalize_biz_name(oe.owner_name_norm)` for maximum coverage.

---

### Phase 3: SOS-derived network enrichment ✅ COMPLETE

Use SOS data to find hidden connections between ownership clusters that parcel-level data doesn't reveal.

**Script:** `scripts/10_sos_network_enrichment.py`

**Results (v2 refined, 2026-02-20):**
- 476,537 → 470,077 clusters = **6,460 net merges**
- New edges: 10,502 shared-RA + 12,451 shared-officer + 23,391 shared-SOS-address = **46,344 total**
- Cluster 1: **4,595 parcels**, Cluster 2: **7,113 parcels**
- Size distribution: 427,530 singletons, 38,418 tiny, 3,906 small, 210 medium, 12 large, 1 mega

**Tuning applied:**
- `BASE_MAX_ADDR_ENTITIES = 10` (down from 100 in script 04) — prevents commercial office park cliques
- `MAX_OFFICER_ENTITIES = 10` — filters attorneys filing for many clients (Rachel Conrad × 145, etc.)
- `MAX_MERGE_PARCELS = 200` — SOS edges only allowed if both base clusters ≤ 200 parcels each
- `commercial_ra` field in SOS data is "No" for all rows — useless. Commercial RAs filtered by name instead (CT Corp, CSC, Cogency, Northwest RA, etc.)

**Two-pass architecture:**
1. Pass 1: Build base graph with `BASE_MAX_ADDR_ENTITIES=10`, compute base cluster assignments + parcel counts
2. Pass 2: Add SOS edges only where `can_merge()` returns True — both endpoints' base clusters ≤ 200 parcels

**Output:** Updated `ownership_clusters` table with merged clusters and SOS summary columns (sos_entity_count, primary_sos_status, primary_foreign_state, registered_agents[]).

---

## Next steps

SOS integration is complete. Remaining work:
1. **Web interface** — see `planning/04_web_interface.md` (Phase 1–5)
2. **Additional Accela record types** — code complaints etc. via `scripts/06_pull_accela_records.py --type`
3. **Residential/homestead filtering** — filter parcels with homestead exemption for renter-occupied focus
4. **Atlanta city enrichment** — council district, NPU, neighborhood via spatial join

---

## Notes

- The SOS dataset is statewide (4.3M entities) — not Atlanta-specific. Matching is the filter.
- `entity_status` values include: Active/Compliance, Admin. Dissolved, Withdrawn, Revoked, etc.
- `business_type_desc` includes: Domestic LLC, Domestic Profit Corp, Foreign LLC, Foreign Profit Corp, etc.
- `foreign_state` is populated only for foreign entities — useful for confirming out-of-state ownership
- The `sos.addresses` table appears to be principal office addresses (not mailing/owner addresses)
