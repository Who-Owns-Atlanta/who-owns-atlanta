"""Enrich ownership clusters using GA SOS data.

Adds new graph edges beyond the parcel-level name/address links from script 04:
  1. Shared non-commercial registered agent
  2. Shared officer (by first+last name)
  3. Shared SOS principal address

Rebuilds the full graph (same edges as script 04 + new SOS edges), re-runs
connected components, and updates cluster_id + ownership_clusters.

Filters to avoid false merges:
  - Known commercial RA names are excluded (they manage thousands of unrelated entities)
  - RA size cap: skip if RA serves > MAX_RA_ENTITIES of our enriched entities
  - Officer size cap: skip if officer appears in > MAX_OFFICER_ENTITIES
  - Address size cap: skip if address shared by > MAX_ADDR_ENTITIES
"""

import re
import networkx as nx
from sqlalchemy import create_engine, text

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

# --- Tuning knobs ---
MAX_RA_ENTITIES     = 30   # skip RA if it manages this many of our entities
MAX_OFFICER_ENTITIES = 10   # skip officer name if appears this many times
                             # (attorneys/accountants filing for many clients hit 50-150;
                             #  legitimate ownership chains are typically < 10)
MAX_ADDR_ENTITIES   = 50   # skip SOS address if this many entities share it

# Known commercial / professional registered agent firms to always skip.
# Normalized to uppercase, no punctuation.
COMMERCIAL_RA_SKIP = {
    "CORPORATION SERVICE COMPANY",
    "C T CORPORATION SYSTEM",
    "CT CORPORATION SYSTEM",
    "COGENCY GLOBAL INC",
    "NORTHWEST REGISTERED AGENT SERVICE INC",
    "NORTHWEST REGISTERED AGENT LLC",
    "REGISTERED AGENTS INC",
    "NATIONAL REGISTERED AGENTS INC",
    "UNITED STATES CORPORATION AGENTS INC",
    "CORPORATE CREATIONS NETWORK INC",
    "CSC OF COBB COUNTY INC",
    "VCORP AGENT SERVICES INC",
    "INCORP SERVICES INC",
    "ANDERSON REGISTERED AGENTS INC",
    "REPUBLIC REGISTERED AGENT LLC",
    "ACCESS MANAGEMENT GROUP",
    "LEGALINC CORPORATE SERVICES INC",
    "PARACORP INC",
    "NONE",
    "",
}

_STRIP_PUNCT = re.compile(r'[^A-Z0-9 ]')

def ra_key(name: str) -> str:
    if not name:
        return ""
    return _STRIP_PUNCT.sub("", name.upper()).strip()

# Same city/zip-only filter as script 04
_CITY_ZIP_ONLY = re.compile(r'^[A-Z]+(\s+[A-Z]+)*\s+[A-Z]{2}\s+\d{5}(-\d+)?$')


# ---------------------------------------------------------------------------
# Load existing owner_entities
# ---------------------------------------------------------------------------

def load_entities(engine):
    print("Loading owner_entities...")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT entity_id, owner_name_norm, owner_addr_norm,
                   sos_control_number, sos_registered_agent_id,
                   sos_registered_agent, sos_match_type
            FROM owner_entities
        """)).fetchall()
    print(f"  {len(rows):,} entities loaded")
    return rows


# ---------------------------------------------------------------------------
# Build graph with existing parcel-level edges (mirrors script 04)
# ---------------------------------------------------------------------------

def build_base_graph(entities):
    print("Building base graph (same_name + same_addr edges)...")
    G = nx.Graph()

    name_idx: dict[str, list[int]] = {}
    addr_idx: dict[str, list[int]] = {}

    for eid, name, addr, *_ in entities:
        G.add_node(eid)
        name_idx.setdefault(name, []).append(eid)
        if addr:
            addr_idx.setdefault(addr, []).append(eid)

    name_edges = 0
    for name, eids in name_idx.items():
        if len(eids) > 1:
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="same_name")
                    name_edges += 1

    addr_edges = 0
    skipped = 0
    for addr, eids in addr_idx.items():
        if _CITY_ZIP_ONLY.match(addr):
            skipped += 1
            continue
        if 1 < len(eids) <= 100:
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="same_addr")
                    addr_edges += 1

    print(f"  {name_edges:,} name edges, {addr_edges:,} addr edges "
          f"({skipped:,} city/zip-only skipped)")
    print(f"  Graph: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")
    return G


# ---------------------------------------------------------------------------
# SOS edge: shared non-commercial registered agent
# ---------------------------------------------------------------------------

def add_ra_edges(G, entities):
    print("Adding SOS registered-agent edges...")

    # Build ra_id → [entity_id] index (trusted matches only)
    ra_idx: dict[str, list[int]] = {}
    ra_names: dict[str, str] = {}
    for eid, name, addr, sos_cn, ra_id, ra_name, match_type in entities:
        if not ra_id or match_type not in ('exact', 'trgm_high'):
            continue
        key = ra_key(ra_name or "")
        if key in COMMERCIAL_RA_SKIP:
            continue
        ra_idx.setdefault(ra_id, []).append(eid)
        ra_names[ra_id] = ra_name or ""

    added = 0
    skipped_large = 0
    for ra_id, eids in ra_idx.items():
        if len(eids) < 2:
            continue
        if len(eids) > MAX_RA_ENTITIES:
            skipped_large += 1
            continue
        for i in range(len(eids)):
            for j in range(i + 1, len(eids)):
                if not G.has_edge(eids[i], eids[j]):
                    G.add_edge(eids[i], eids[j], rel="shared_ra",
                               ra=ra_names[ra_id])
                    added += 1

    print(f"  {added:,} shared-RA edges added "
          f"({skipped_large:,} RAs skipped — too large)")
    return added


# ---------------------------------------------------------------------------
# SOS edge: shared officer (by normalized first+last name)
# ---------------------------------------------------------------------------

def add_officer_edges(G, engine, entities):
    print("Loading officer data for enriched entities...")

    # Only process trusted-matched entities that have a control_number
    enriched = {
        eid: cn
        for eid, name, addr, cn, ra_id, ra_name, match_type in entities
        if cn and match_type in ('exact', 'trgm_high')
    }

    if not enriched:
        print("  No enriched entities found — skipping")
        return 0

    # Fetch officers for these control numbers from the DB
    # Use a temp table to avoid a massive IN clause
    with engine.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE _enrich_cns (control_number TEXT) ON COMMIT DROP"))
        cns = list({cn for cn in enriched.values() if cn})
        CHUNK = 5000
        for i in range(0, len(cns), CHUNK):
            conn.execute(
                text("INSERT INTO _enrich_cns VALUES (:cn)"),
                [{"cn": cn} for cn in cns[i:i+CHUNK]]
            )

        rows = conn.execute(text("""
            SELECT o.control_number,
                   upper(trim(o.first_name)) AS fn,
                   upper(trim(o.last_name))  AS ln
            FROM sos.officers o
            JOIN _enrich_cns ec ON ec.control_number = o.control_number
            WHERE o.first_name IS NOT NULL AND trim(o.first_name) <> ''
              AND o.last_name  IS NOT NULL AND trim(o.last_name)  <> ''
        """)).fetchall()

    print(f"  {len(rows):,} officer records for {len(cns):,} SOS entities")

    # Build: (first, last) → set of control_numbers
    officer_cns: dict[tuple, set] = {}
    for cn, fn, ln in rows:
        if fn and ln and len(ln) > 1:    # skip single-char names
            officer_cns.setdefault((fn, ln), set()).add(cn)

    # Build: control_number → [entity_ids]
    cn_to_eids: dict[str, list[int]] = {}
    for eid, cn in enriched.items():
        cn_to_eids.setdefault(cn, []).append(eid)

    added = 0
    skipped_large = 0
    for (fn, ln), cns_for_officer in officer_cns.items():
        # Collect all entity_ids for entities sharing this officer
        eids_for_officer = []
        for cn in cns_for_officer:
            eids_for_officer.extend(cn_to_eids.get(cn, []))

        if len(eids_for_officer) < 2:
            continue
        if len(eids_for_officer) > MAX_OFFICER_ENTITIES:
            skipped_large += 1
            continue

        for i in range(len(eids_for_officer)):
            for j in range(i + 1, len(eids_for_officer)):
                if not G.has_edge(eids_for_officer[i], eids_for_officer[j]):
                    G.add_edge(eids_for_officer[i], eids_for_officer[j],
                               rel="shared_officer", officer=f"{fn} {ln}")
                    added += 1

    print(f"  {added:,} shared-officer edges added "
          f"({skipped_large:,} officers skipped — too large)")
    return added


# ---------------------------------------------------------------------------
# SOS edge: shared SOS principal address
# ---------------------------------------------------------------------------

def add_sos_addr_edges(G, engine, entities):
    print("Loading SOS principal addresses for enriched entities...")

    enriched_cns = {
        eid: cn
        for eid, name, addr, cn, ra_id, ra_name, match_type in entities
        if cn and match_type in ('exact', 'trgm_high')
    }

    if not enriched_cns:
        return 0

    with engine.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE _enrich_cns2 (control_number TEXT) ON COMMIT DROP"))
        cns = list({cn for cn in enriched_cns.values() if cn})
        CHUNK = 5000
        for i in range(0, len(cns), CHUNK):
            conn.execute(
                text("INSERT INTO _enrich_cns2 VALUES (:cn)"),
                [{"cn": cn} for cn in cns[i:i+CHUNK]]
            )

        rows = conn.execute(text("""
            SELECT a.control_number,
                   upper(trim(a.street_address1)) AS street,
                   upper(trim(a.city))            AS city,
                   upper(trim(a.state))           AS state
            FROM sos.addresses a
            JOIN _enrich_cns2 ec ON ec.control_number = a.control_number
            WHERE a.street_address1 IS NOT NULL AND trim(a.street_address1) <> ''
              AND a.city IS NOT NULL AND trim(a.city) <> ''
        """)).fetchall()

    print(f"  {len(rows):,} SOS address records")

    # Build: (street, city, state) → set of control_numbers
    addr_cns: dict[tuple, set] = {}
    for cn, street, city, state in rows:
        if street and city:
            addr_cns.setdefault((street, city, state or ''), set()).add(cn)

    # Build: control_number → [entity_ids]
    cn_to_eids: dict[str, list[int]] = {}
    for eid, cn in enriched_cns.items():
        cn_to_eids.setdefault(cn, []).append(eid)

    added = 0
    skipped_large = 0
    for (street, city, state), cns_for_addr in addr_cns.items():
        eids_for_addr = []
        for cn in cns_for_addr:
            eids_for_addr.extend(cn_to_eids.get(cn, []))

        if len(eids_for_addr) < 2:
            continue
        if len(eids_for_addr) > MAX_ADDR_ENTITIES:
            skipped_large += 1
            continue

        for i in range(len(eids_for_addr)):
            for j in range(i + 1, len(eids_for_addr)):
                if not G.has_edge(eids_for_addr[i], eids_for_addr[j]):
                    G.add_edge(eids_for_addr[i], eids_for_addr[j],
                               rel="shared_sos_addr",
                               addr=f"{street}, {city}")
                    added += 1

    print(f"  {added:,} shared-SOS-address edges added "
          f"({skipped_large:,} addresses skipped — too large)")
    return added


# ---------------------------------------------------------------------------
# Re-cluster and write back
# ---------------------------------------------------------------------------

def reassign_clusters(engine, G):
    print("Finding connected components...")
    components = list(nx.connected_components(G))
    components.sort(key=len, reverse=True)
    print(f"  {len(components):,} clusters")

    cluster_map = {}
    for cluster_id, component in enumerate(components, 1):
        for eid in component:
            cluster_map[eid] = cluster_id

    print("Writing cluster assignments...")
    with engine.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE tmp_clusters (entity_id BIGINT, cluster_id INT)"))

        updates = [{"eid": eid, "cid": cid} for eid, cid in cluster_map.items()]
        CHUNK = 50000
        for i in range(0, len(updates), CHUNK):
            conn.execute(
                text("INSERT INTO tmp_clusters VALUES (:eid, :cid)"),
                updates[i:i+CHUNK]
            )

        conn.execute(text("""
            UPDATE owner_entities oe
            SET cluster_id = tc.cluster_id
            FROM tmp_clusters tc
            WHERE oe.entity_id = tc.entity_id
        """))
        conn.execute(text("DROP TABLE tmp_clusters"))

    print("Rebuilding ownership_clusters summary...")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ownership_clusters CASCADE"))
        conn.execute(text("""
            CREATE TABLE ownership_clusters AS
            SELECT
                cluster_id,
                COUNT(*)                                                    AS entity_count,
                SUM(count)                                                  AS parcel_count,
                ARRAY_AGG(DISTINCT owner_name_norm ORDER BY owner_name_norm) AS owner_names,
                ARRAY_AGG(DISTINCT owner_addr_norm ORDER BY owner_addr_norm)
                    FILTER (WHERE owner_addr_norm != '')                    AS owner_addresses,
                -- SOS enrichment summary
                COUNT(DISTINCT sos_control_number)
                    FILTER (WHERE sos_control_number IS NOT NULL)           AS sos_entity_count,
                MODE() WITHIN GROUP (ORDER BY sos_status)                  AS primary_sos_status,
                MODE() WITHIN GROUP (ORDER BY sos_foreign_state)
                    FILTER (WHERE sos_foreign_state IS NOT NULL
                              AND sos_foreign_state <> '')                  AS primary_foreign_state,
                ARRAY_AGG(DISTINCT sos_registered_agent ORDER BY sos_registered_agent)
                    FILTER (WHERE sos_registered_agent IS NOT NULL
                              AND sos_registered_agent <> '')               AS registered_agents
            FROM owner_entities
            GROUP BY cluster_id
            ORDER BY parcel_count DESC
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oc_cluster ON ownership_clusters (cluster_id)"))

    return len(components)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(engine, n_clusters_before):
    with engine.connect() as conn:
        n_clusters_after = conn.execute(text("SELECT count(*) FROM ownership_clusters")).scalar()
        merged = n_clusters_before - n_clusters_after

        top = conn.execute(text("""
            SELECT cluster_id, parcel_count, entity_count, sos_entity_count,
                   primary_sos_status, primary_foreign_state,
                   owner_names[1:3]
            FROM ownership_clusters
            ORDER BY parcel_count DESC
            LIMIT 20
        """)).fetchall()

        multi_sos = conn.execute(text("""
            SELECT count(*) FROM ownership_clusters WHERE sos_entity_count > 1
        """)).scalar()

    print(f"\n--- Cluster changes ---")
    print(f"  Before: {n_clusters_before:,} clusters")
    print(f"  After:  {n_clusters_after:,} clusters")
    print(f"  Merged: {merged:,} clusters collapsed by SOS edges")

    print(f"\n  {multi_sos:,} clusters with 2+ SOS-linked entities")

    print(f"\nTop 20 ownership clusters after SOS enrichment:")
    for r in top:
        names = (r.owner_names or [])[:3]
        status = r.primary_sos_status or ""
        state = f" [{r.primary_foreign_state}]" if r.primary_foreign_state else ""
        print(f"  Cluster {r.cluster_id:>6}: {r.parcel_count:>5} parcels, "
              f"{r.entity_count} entities, {r.sos_entity_count or 0} SOS"
              f"{state} {status} — {names}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Count clusters before
    with engine.connect() as conn:
        n_before = conn.execute(text("SELECT count(*) FROM ownership_clusters")).scalar()
    print(f"Clusters before SOS enrichment: {n_before:,}")

    entities = load_entities(engine)

    G = build_base_graph(entities)

    ra_added      = add_ra_edges(G, entities)
    officer_added = add_officer_edges(G, engine, entities)
    addr_added    = add_sos_addr_edges(G, engine, entities)

    total_new = ra_added + officer_added + addr_added
    print(f"\nTotal new SOS edges: {total_new:,}")
    print(f"Graph now: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")

    n_after = reassign_clusters(engine, G)
    print_stats(engine, n_before)
    print("\nDone.")


if __name__ == "__main__":
    main()
