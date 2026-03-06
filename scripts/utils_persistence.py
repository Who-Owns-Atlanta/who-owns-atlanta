from sqlalchemy import create_engine, text
from multiprocessing import Pool, cpu_count

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_persistence_schema(engine):
    """Ensure persistence tables exist. Safe to call on every pipeline run."""
    with engine.begin() as conn:
        # 1. Entity Registry: maps (name, addr, county) -> stable entity_id
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS entity_registry (
                entity_id SERIAL PRIMARY KEY,
                name_norm TEXT,
                addr_norm TEXT,
                county TEXT,
                first_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                last_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(name_norm, addr_norm, county)
            );
        """))

        # 2. Cluster Registry: sequence anchor — IDs only go up, never reused
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cluster_registry (
                cluster_id SERIAL PRIMARY KEY,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """))

        # 3. Entity-Cluster History: each entity's cluster assignment from last run
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS entity_cluster_history (
                entity_id BIGINT PRIMARY KEY,
                cluster_id INT NOT NULL,
                parcel_count INT NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """))


# ---------------------------------------------------------------------------
# One-time migration: seed from current data (run once, then delete caller)
# ---------------------------------------------------------------------------

def seed_persistence_from_current(engine):
    """
    One-time seed of persistence tables from current owner_entities.
    Preserves existing cluster IDs as the stable baseline.
    Must be called AFTER 04_ownership_network.py has created owner_entities.
    """
    print("Seeding persistence tables from current owner_entities...")
    with engine.begin() as conn:
        # Seed entity_registry (preserving existing entity_ids)
        conn.execute(text("""
            INSERT INTO entity_registry (entity_id, name_norm, addr_norm, county)
            SELECT entity_id, owner_name_norm, owner_addr_norm, county
            FROM owner_entities
            ON CONFLICT (name_norm, addr_norm, county) DO NOTHING;
        """))
        print(f"  entity_registry: seeded")

        # Advance sequence past the seeded IDs
        conn.execute(text(
            "SELECT setval('entity_registry_entity_id_seq', "
            "(SELECT MAX(entity_id) FROM entity_registry))"
        ))

        # Seed cluster_registry (one row per distinct cluster_id)
        conn.execute(text("""
            INSERT INTO cluster_registry (cluster_id)
            SELECT DISTINCT cluster_id FROM owner_entities
            ON CONFLICT (cluster_id) DO NOTHING;
        """))
        conn.execute(text(
            "SELECT setval('cluster_registry_cluster_id_seq', "
            "(SELECT MAX(cluster_id) FROM cluster_registry))"
        ))
        print(f"  cluster_registry: seeded")

        # Seed entity_cluster_history — all entities
        conn.execute(text("""
            INSERT INTO entity_cluster_history (entity_id, cluster_id, parcel_count)
            SELECT entity_id, cluster_id, count
            FROM owner_entities
            ON CONFLICT (entity_id) DO UPDATE
                SET cluster_id   = EXCLUDED.cluster_id,
                    parcel_count = EXCLUDED.parcel_count,
                    updated_at   = NOW();
        """))
        count = conn.execute(text(
            "SELECT COUNT(*) FROM entity_cluster_history"
        )).scalar()
        print(f"  entity_cluster_history: {count:,} rows")
    print("  Seed complete.")


# ---------------------------------------------------------------------------
# Parallel matching helpers (used for multi-parcel clusters)
# ---------------------------------------------------------------------------

_eid_to_history = {}  # module-level: populated by pool initializer


def _init_worker(hist):
    global _eid_to_history
    _eid_to_history = hist


def _match_cluster(args):
    """Worker: compute best old cluster_id for one new cluster via weighted vote."""
    new_cid, members = args
    old_id_weights = {}
    for eid, count in members:
        if eid in _eid_to_history:
            old_cid = _eid_to_history[eid][0]
            old_id_weights[old_cid] = old_id_weights.get(old_cid, 0) + count
    best_old_cid = max(old_id_weights, key=old_id_weights.get) if old_id_weights else None
    return new_cid, best_old_cid


# ---------------------------------------------------------------------------
# Core: reassign cluster IDs for stability across runs
# ---------------------------------------------------------------------------

def reassign_cluster_ids(engine):
    """
    Match new clusters against history and reassign cluster_ids to persist them.
    Called at the end of 10b_cluster_refinement.py after fission/fusion.

    Algorithm: weighted voting — each entity votes for its old cluster_id,
    weighted by parcel_count. Largest clusters get priority for claiming IDs.
    Unmatched clusters (new owners, split losers) get fresh IDs from cluster_registry.

    Multi-parcel clusters (2+ parcels): parallel Pool matching.
    Singleton clusters: direct O(1) dict lookup per cluster (no Pool overhead).
    """
    print("\n=== Persistence: Reassigning Cluster IDs ===")

    with engine.connect() as conn:
        history = conn.execute(text(
            "SELECT entity_id, cluster_id, parcel_count FROM entity_cluster_history"
        )).fetchall()
        eid_to_history = {r[0]: (r[1], r[2]) for r in history}
        print(f"  Loaded {len(eid_to_history):,} history entries")

        current = conn.execute(text(
            "SELECT entity_id, cluster_id, count FROM owner_entities"
        )).fetchall()

    entities_by_new_cid = {}
    for eid, new_cid, count in current:
        entities_by_new_cid.setdefault(new_cid, []).append((eid, count))

    # Separate multi-parcel clusters from singletons for different matching paths
    multi_items = [
        (cid, members) for cid, members in entities_by_new_cid.items()
        if sum(e[1] for e in members) >= 2
    ]
    singleton_items = [
        (cid, members) for cid, members in entities_by_new_cid.items()
        if sum(e[1] for e in members) < 2
    ]

    # Sort largest multi-parcel clusters first so they get priority when claiming old IDs
    multi_items.sort(key=lambda kv: sum(e[1] for e in kv[1]), reverse=True)

    # Parallel weighted-vote matching for multi-parcel clusters
    print(f"  Matching {len(multi_items):,} multi-parcel clusters (parallel)...")
    with Pool(cpu_count(), initializer=_init_worker, initargs=(eid_to_history,)) as pool:
        match_results = pool.map(_match_cluster, multi_items)

    new_cid_to_persistent_id = {}
    used_persistent_ids = set()
    unmatched_new_cids = []

    for new_cid, best_old_cid in match_results:
        if best_old_cid is not None and best_old_cid not in used_persistent_ids:
            new_cid_to_persistent_id[new_cid] = best_old_cid
            used_persistent_ids.add(best_old_cid)
        else:
            unmatched_new_cids.append(new_cid)

    # Direct lookup for singletons (1-entity, O(1) per cluster, no Pool overhead)
    print(f"  Matching {len(singleton_items):,} singleton clusters (direct lookup)...")
    for new_cid, members in singleton_items:
        eid = members[0][0]
        if eid in eid_to_history:
            old_cid = eid_to_history[eid][0]
            if old_cid not in used_persistent_ids:
                new_cid_to_persistent_id[new_cid] = old_cid
                used_persistent_ids.add(old_cid)
                continue
        unmatched_new_cids.append(new_cid)

    # Assign fresh IDs for new/unmatched clusters
    with engine.begin() as conn:
        max_id = conn.execute(text(
            "SELECT MAX(cluster_id) FROM cluster_registry"
        )).scalar() or 0
        next_id = max_id + 1

        new_registry_rows = [{"cid": next_id + i} for i in range(len(unmatched_new_cids))]
        for i, new_cid in enumerate(unmatched_new_cids):
            new_cid_to_persistent_id[new_cid] = next_id + i

        if new_registry_rows:
            conn.execute(
                text("INSERT INTO cluster_registry (cluster_id) VALUES (:cid)"),
                new_registry_rows,
            )
            final_id = next_id + len(unmatched_new_cids) - 1
            conn.execute(text(
                "SELECT setval('cluster_registry_cluster_id_seq', :v)"
            ), {"v": final_id})

    matched = len(new_cid_to_persistent_id) - len(unmatched_new_cids)
    print(f"  {matched:,} clusters matched to history")
    print(f"  {len(unmatched_new_cids):,} clusters assigned new IDs")

    # Apply reassignments to owner_entities (skip no-ops where id already correct)
    updates = [
        {"old_cid": k, "new_cid": v}
        for k, v in new_cid_to_persistent_id.items()
        if k != v
    ]
    if updates:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TEMP TABLE tmp_id_map (old_cid INT, new_cid INT)"
            ))
            CHUNK = 50000
            for i in range(0, len(updates), CHUNK):
                conn.execute(
                    text("INSERT INTO tmp_id_map VALUES (:old_cid, :new_cid)"),
                    updates[i:i + CHUNK],
                )
            conn.execute(text("""
                UPDATE owner_entities oe
                SET cluster_id = m.new_cid
                FROM tmp_id_map m
                WHERE oe.cluster_id = m.old_cid
            """))
        print(f"  Applied {len(updates):,} cluster_id reassignments to owner_entities")

    # Update history for next run — all entities
    print("  Updating entity_cluster_history for next run...")
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE entity_cluster_history"))
        conn.execute(text("""
            INSERT INTO entity_cluster_history (entity_id, cluster_id, parcel_count)
            SELECT entity_id, cluster_id, count FROM owner_entities
        """))
        hist_count = conn.execute(text(
            "SELECT COUNT(*) FROM entity_cluster_history"
        )).scalar()
        print(f"  entity_cluster_history updated: {hist_count:,} rows")
    print("  Persistence update complete.")
