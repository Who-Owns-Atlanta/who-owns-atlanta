# Plan 22 — Clustering Accuracy Fixes

**Type B data release** → target `v202603B.1`

---

## Background

Two independent clustering bugs were identified — one over-merging unrelated
individuals, one under-merging the same joint owners listed in different field order.
Both require a full pipeline re-run.

---

## Bug 1: Over-merging individuals via middle-initial stripping

### Root cause

`scripts/10b_cluster_refinement.py` — Pass A (Name-Series Fusion)

`is_strippable()` treats any single trailing letter as noise to strip (designed
for corporate series suffixes like "FUND SERIES B LLC"). This also strips
meaningful middle initials from individual names:

- `OWNER FIRSTNAME J` → stem `OWNER FIRSTNAME`
- `OWNER FIRSTNAME E` → stem `OWNER FIRSTNAME`

Pass A's query selected `WHERE is_institutional = FALSE`, which included all
individuals — not just corporate entities that Pass A was designed for.

Result: two unrelated individuals with similar first-last names and the same
city/state were incorrectly fused into one cluster.

### Fix

`scripts/10b_cluster_refinement.py` line ~312 — added `AND is_corporate = TRUE`:

```sql
WHERE is_institutional = FALSE AND is_corporate = TRUE
```

Pass A now only attempts stem-based fusion on flagged corporate entities,
leaving individual names untouched.

### Prerequisite

`owner_entities` did not have an `is_corporate` column. Added `BOOL_OR(is_corporate)`
aggregation in `scripts/04_ownership_network.py` alongside the existing
`BOOL_OR(is_institutional)` line.

---

## Bug 2: Under-merging joint owners in different field order

### Root cause

`scripts/04_ownership_network.py` — entity normalization

`owner_name_norm` was built as `UPPER(TRIM(owner_name))`, which is order-sensitive.
When county data lists the same two co-owners in reversed order on different parcels:

- Parcel A: `PERSON ONE & PERSON TWO`
- Parcel B: `PERSON TWO & PERSON ONE`

These produced different `owner_name_norm` values, different entity rows, no
shared-name edge in the network, and separate clusters — even though they are
clearly the same ownership.

### Fix

`scripts/04_ownership_network.py` — replaced `UPPER(TRIM(owner_name))` with a
PostgreSQL expression that sorts joint components alphabetically:

```sql
CASE
  WHEN UPPER(TRIM(owner_name)) LIKE '% & %'
  THEN (
    SELECT STRING_AGG(part, ' & ' ORDER BY part)
    FROM UNNEST(STRING_TO_ARRAY(UPPER(TRIM(owner_name)), ' & ')) AS part
  )
  ELSE UPPER(TRIM(owner_name))
END AS owner_name_norm
```

Applied in both the SELECT and GROUP BY of `CREATE TEMP TABLE tmp_raw_entities`.

The same expression in GROUP BY ensures consistent aggregation; the CASE is
evaluated identically in both positions.

**Edge cases:**
- Multiple `&` parts (`A & B & C`) — all parts sorted, correct behavior
- `ET AL` names — unaffected (only splits on ` & `)
- Corporate joint names (`SMITH LLC & JONES LLC`) — sorting is harmless

**Note:** This changes `owner_name_norm` for all co-ownership records that
contain `&`. The `entity_registry` uses `(name_norm, addr_norm, county)` as a
stable ID key — affected entities will receive new `entity_id` values.
Acceptable in a Type B release.

---

## Files Changed

- `scripts/04_ownership_network.py` — joint-name sort + `is_corporate` column
- `scripts/10b_cluster_refinement.py` — Pass A restricted to `is_corporate = TRUE`

---

## Verification Queries

After re-running the pipeline:

```sql
-- Bug 1: Variants with different middle initials should be in separate clusters
-- (unless they actually share an address)
SELECT owner_name_norm, owner_addr_norm, cluster_id
FROM owner_entities
WHERE owner_name_norm LIKE 'COMMON LASTNAME FIRSTNAME%'
  AND owner_addr_norm ILIKE '%city%'
ORDER BY cluster_id, owner_name_norm;

-- Bug 2: Joint owners with reversed field order should share a cluster
SELECT owner_name_norm, owner_addr_norm, cluster_id
FROM owner_entities
WHERE owner_name_norm LIKE '%PERSON ONE%' AND owner_name_norm LIKE '%PERSON TWO%';
-- Expect: single cluster_id for both records
```
