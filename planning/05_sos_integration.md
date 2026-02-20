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

Match ~45K distinct corporate owner names from `owner_entities` against `sos.entities.business_name`.

**Script:** `scripts/08_match_sos.py`

**Strategy (in priority order):**
1. Exact normalized match (uppercase, stripped) — highest confidence
2. Trigram similarity ≥ 0.85 — catches minor typos
3. Trigram similarity 0.70–0.85 — lower confidence, flag for review

**Output table:** `public.sos_matches`
```sql
CREATE TABLE sos_matches (
    owner_name        TEXT,         -- from owner_entities.owner_name
    sos_control_number TEXT,
    sos_business_name  TEXT,
    match_type        TEXT,         -- 'exact', 'trgm_high', 'trgm_low'
    similarity        FLOAT,
    business_type     TEXT,
    entity_status     TEXT,
    foreign_state     TEXT
);
```

**Scope:** Only match owners where `is_corporate = TRUE`. Skip institutional (government, HOAs).

**Actual match results (2026-02-19):**

| Match type | Count | Notes |
|---|---|---|
| exact (1.0) | 13,264 | Normalized exact match |
| trgm_high ≥0.95 | 19,744 | Near-perfect — punctuation/abbrev diffs |
| trgm_high 0.85–0.95 | 3,814 | High confidence |
| trgm_high 0.80–0.85 | 1,534 | Use with caution — some false positives |
| trgm_low 0.65–0.79 | 3,271 | Low confidence — flag only, don't trust |
| Unmatched | 3,268 | Genuinely out-of-state or no SOS record |
| **Total** | **44,431** | **92.6% matched; 82.9% trusted (≥0.85)** |

**Implementation notes:**
- Prefix-blocking approach: group 4.2M SOS names by first 5 chars, compare parcel name against same-prefix bucket only
- `rapidfuzz.fuzz.token_sort_ratio` with extra punctuation normalization (hyphens, commas stripped before compare)
- One-time runtime: ~5 minutes
- Known issue: trgm_high at exactly 0.80 has false positives from prefix coincidences
- `trgm_low` should not be used for network enrichment — too many false positives

---

### Phase 2: Enrich owner_entities with SOS data ✅ COMPLETE

Once matches are confirmed, propagate SOS fields back to `owner_entities`:

```sql
ALTER TABLE owner_entities ADD COLUMN IF NOT EXISTS sos_control_number TEXT;
ALTER TABLE owner_entities ADD COLUMN IF NOT EXISTS sos_status TEXT;
ALTER TABLE owner_entities ADD COLUMN IF NOT EXISTS sos_business_type TEXT;
ALTER TABLE owner_entities ADD COLUMN IF NOT EXISTS sos_foreign_state TEXT;
ALTER TABLE owner_entities ADD COLUMN IF NOT EXISTS sos_registered_agent TEXT;
ALTER TABLE owner_entities ADD COLUMN IF NOT EXISTS sos_principal_city TEXT;
ALTER TABLE owner_entities ADD COLUMN IF NOT EXISTS sos_principal_state TEXT;
```

**Script:** `scripts/09_enrich_owners_sos.py`

**Results (2026-02-19):**
- 48,579 owner_entities enriched (exact + trgm_high ≥0.80 only; trgm_low skipped)
- 42,410 active entities, 4,464 admin dissolved, 30,083 foreign-incorporated
- Delaware: 4,874 (expected — LLC formation state)
- Top RAs are all commercial (CSC, CT Corp, Cogency) — need filtering in Phase 3
- Join uses `normalize_biz_name(oe.owner_name_norm)` to catch ~1,600 extra matches vs direct equality

---

### Phase 3: SOS-derived network enrichment

Use SOS data to find hidden connections between ownership clusters that parcel-level data doesn't reveal:

**Connection types to discover:**
1. **Shared registered agent** — two clusters use the same non-commercial RA (individual RAs signal control)
2. **Shared officer name** — same person as officer/director of multiple entities
3. **Shared principal address** — same office address for multiple entities (even if name differs)

**Script:** `scripts/10_sos_network_enrichment.py`

**Approach:**
- Filter out commercial RAs (e.g. CT Corporation, Northwest Registered Agent, Cogency) — they manage thousands of unrelated entities and would create false links
- Filter out officer names that are very common or blank
- Generate new edges in the ownership graph, re-run cluster assignment

**Output:** Updated `ownership_clusters` table with merged clusters where SOS evidence supports it.

---

## Next steps (ordered)

1. **`scripts/08_match_sos.py`** — name matching, create `sos_matches` table ← START HERE
2. **Review match quality** — spot-check exact matches, tune trgm threshold
3. **`scripts/09_enrich_owners_sos.py`** — populate SOS columns on `owner_entities`
4. **`scripts/10_sos_network_enrichment.py`** — graph enrichment via shared agents/officers
5. **Update `02_project_status.md`** — mark SOS integration complete

---

## Notes

- The SOS dataset is statewide (4.3M entities) — not Atlanta-specific. Matching is the filter.
- `entity_status` values include: Active/Compliance, Admin. Dissolved, Withdrawn, Revoked, etc.
- `business_type_desc` includes: Domestic LLC, Domestic Profit Corp, Foreign LLC, Foreign Profit Corp, etc.
- `foreign_state` is populated only for foreign entities — useful for confirming out-of-state ownership
- The `sos.addresses` table appears to be principal office addresses (not mailing/owner addresses)
