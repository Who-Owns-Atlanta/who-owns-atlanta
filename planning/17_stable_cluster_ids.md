# Stable Cluster & Entity IDs Across Pipeline Runs

## Problem

Every full pipeline run regenerates `entity_id` and `cluster_id` from scratch:

- `entity_id`: assigned via `ROW_NUMBER() OVER ()` with no ORDER BY — non-deterministic, changes every run
- `cluster_id`: assigned as `enumerate(components, 1)` sorted by component size DESC — any change to parcel data reshuffles the entire rank order

This breaks all permalink surfaces: `/api/owner/{cluster_id}`, `?cluster=ID` deep links, static owner pages, and any shared URLs.

## Solution

Two-part fix: stable entity IDs via a natural-key registry, and stable cluster IDs via weighted-vote history matching across runs.

---

## New Tables

### `entity_registry` (exists in DB, currently empty)

```sql
entity_id   SERIAL PRIMARY KEY,
name_norm   TEXT,
addr_norm   TEXT,
county      TEXT,
first_seen  TIMESTAMPTZ DEFAULT NOW(),
last_seen   TIMESTAMPTZ DEFAULT NOW(),
UNIQUE(name_norm, addr_norm, county)
```

The natural key `(name_norm, addr_norm, county)` is unique across all entities (verified — zero duplicates in 523,555 current entities). Each pipeline run upserts into this table and joins back to retrieve a stable `entity_id` rather than generating one from row order.

### `cluster_registry` (new)

```sql
cluster_id  SERIAL PRIMARY KEY,
created_at  TIMESTAMPTZ DEFAULT NOW()
```

Sequence anchor. Ensures cluster IDs only increase and are never reused. Dissolved clusters remain as tombstones — old URLs 404 cleanly instead of returning the wrong owner's data.

### `entity_cluster_history` (new)

```sql
entity_id    BIGINT PRIMARY KEY,
cluster_id   INT NOT NULL,
parcel_count INT NOT NULL,
updated_at   TIMESTAMPTZ DEFAULT NOW()
```

Stores each entity's cluster assignment from the previous run (~524K rows — all entities). This is the cross-run matching signal.

Note: all entities are included (not just multi-parcel clusters). Singletons share the same integer ID space as multi-parcel clusters — excluding them from history causes them to receive new IDs each run, which would collide with stable multi-parcel IDs in `owner_entities`. Including all entities costs ~12MB and keeps everything collision-free.

---

## Pipeline Changes

### `scripts/04_ownership_network.py` — `build_owner_entities()`

Replace `ROW_NUMBER() OVER () AS entity_id` with:

1. Call `ensure_persistence_schema(engine)` at startup (idempotent — safe every run)
2. Build raw entity data into a temp table
3. UPSERT into `entity_registry` on `(name_norm, addr_norm, county)`, touch `last_seen`
4. JOIN back to `entity_registry` to retrieve stable `entity_id`
5. Create `owner_entities` from the JOIN result

### `scripts/10b_cluster_refinement.py` — end of `__main__`

After `rebuild_ownership_clusters()`:

1. Call `reassign_cluster_ids(engine)` from `utils_persistence`
2. Call `rebuild_ownership_clusters(engine)` a second time so the table reflects the now-stable IDs

---

## Cluster ID Matching Algorithm (`reassign_cluster_ids`)

Runs at the end of each full clustering pipeline (after fission/fusion):

1. Load `entity_cluster_history`: `entity_id → (old_cluster_id, parcel_count)`
2. Split clusters into two groups:
   - **Multi-parcel** (≥2 total parcels, ~37K): sorted largest-first so big clusters claim ancestors first
   - **Singletons** (~430K): direct O(1) dict lookup per cluster (no Pool overhead)
3. **[Parallel]** Multi-parcel clusters: `multiprocessing.Pool` weighted-vote matching — each entity votes for its old `cluster_id` weighted by `parcel_count`; pool initializer shares the history dict once per worker (avoids per-task pickling of the 524K-entry dict)
4. **[Sequential]** Singletons: direct `eid_to_history` dict lookup (1 entity per cluster)
5. Collect all results; assign IDs with largest-first priority for unclaimed old IDs; unmatched clusters get fresh IDs from `cluster_registry` SERIAL
6. Apply reassignments to `owner_entities` in 50K-row batched chunks
7. TRUNCATE and repopulate `entity_cluster_history` with all entities for the next run

### Edge cases

| Scenario | Behavior |
|---|---|
| Incremental parcel add/remove | Weighted majority still votes for old ID — stable |
| Cluster split | Larger piece inherits old ID; smaller gets new ID |
| Cluster merge | New combined cluster inherits the larger contributor's ID |
| Entirely new owner | No history match → fresh SERIAL from `cluster_registry` |
| Dissolved cluster | Tombstone stays in `cluster_registry`; API returns 404 |

---

## One-Time Migration — COMPLETED

The registry tables were seeded from the existing `owner_entities` to preserve current cluster IDs as the stable baseline. `scripts/initialize_persistence.py` was deleted after successful execution. The system is self-sustaining from this point.

### Fresh-install behavior

On a new database, no migration is needed. `ensure_persistence_schema()` creates empty tables; `reassign_cluster_ids()` finds empty history and assigns new IDs on the first run; those IDs are stored in history and preserved from the second run onward.

---

## Files

**Modified:**
- `scripts/04_ownership_network.py` — `build_owner_entities()`
- `scripts/10b_cluster_refinement.py` — `__main__` block
- `scripts/utils_persistence.py` — fix `ensure_persistence_schema` DDL (add `first_seen`/`last_seen` to `entity_registry`); parallelize cluster matching loop; add `parcel_count >= 2` filter to history write

**Delete after migration:**
- `scripts/initialize_persistence.py`

---

## Verification — COMPLETED

Row counts confirmed post-migration:

```
entity_registry:        523,555 rows  (sequence last_value=523555)
cluster_registry:       898,417 rows  (sequence last_value=898417)
entity_cluster_history: 523,555 rows
```

Note: `cluster_registry` max cluster_id is 898,417 (sequence reflects the test run during
development that temporarily assigned new IDs to singletons). The 430K stale tombstones
were cleaned up; the table has exactly 467,581 rows — one per active cluster.

`reassign_cluster_ids` confirmed no-op on current state:
- 467,581 clusters matched to history
- 0 new IDs assigned
- Top cluster IDs intact: 7=SFR XII (2930), 3=HOME SFR (2490), 1=LWH CAREY PARK (2220)
