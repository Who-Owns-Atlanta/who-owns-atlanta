"""Enrich ownership clusters using GA SOS data.

TWO-PASS DESIGN to prevent mega-cluster expansion:

Pass 1 — Base graph (tight address cap):
  Rebuild name + address edges as script 04, but with BASE_MAX_ADDR_ENTITIES = 10.
  The original cap of 100 allowed office park addresses (92 entities at one address)
  to form massive cliques. Capping at 10 keeps only genuine small-portfolio linkages.
  Compute base cluster assignments and parcel counts.

Pass 2 — SOS edges (size-gated):
  Add three SOS-derived edge types:
    1. Shared non-commercial registered agent
    2. Shared officer (by first+last name)
    3. Shared SOS principal address
  A SOS edge is only added if BOTH endpoints' base clusters have <= MAX_MERGE_PARCELS
  parcels. This allows SOS to merge small portfolios but never expands a large cluster.

Final: re-run connected components, update cluster_id + ownership_clusters.
"""

import re
import networkx as nx
from sqlalchemy import create_engine, text

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

# --- Tuning knobs ---
BASE_MAX_ADDR_ENTITIES = 10   # base graph: skip address if shared by more entities
                               # (100 in script 04 allowed office-park mega-clusters)
MAX_RA_ENTITIES        = 30   # skip RA if it manages this many of our entities
MAX_OFFICER_ENTITIES   = 10   # skip officer if appears this many times among our entities
MAX_SOS_ADDR_ENTITIES  = 20   # skip SOS address if this many entities share it
MAX_MERGE_PARCELS      = 200  # SOS edge gate: skip if either base cluster > this many parcels

# Known commercial / professional registered agent firms — always skip for RA edges.
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
_CITY_ZIP_ONLY = re.compile(r'^[A-Z]+(\s+[A-Z]+)*\s+[A-Z]{2}\s+\d{5}(-\d+)?$')


def ra_key(name: str) -> str:
    if not name:
        return ""
    return _STRIP_PUNCT.sub("", name.upper()).strip()


# ---------------------------------------------------------------------------
# Load entities
# ---------------------------------------------------------------------------

def load_entities(engine):
    print("Loading owner_entities...")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT entity_id, owner_name_norm, owner_addr_norm, count,
                   sos_control_number, sos_registered_agent_id,
                   sos_registered_agent, sos_match_type
            FROM owner_entities
        """)).fetchall()
    print(f"  {len(rows):,} entities loaded")
    return rows


# ---------------------------------------------------------------------------
# Pass 1: base graph with tight address cap
# ---------------------------------------------------------------------------

def build_base_graph(entities):
    print(f"\nPass 1: base graph (address cap = {BASE_MAX_ADDR_ENTITIES})...")
    G = nx.Graph()
    name_idx: dict[str, list[int]] = {}
    addr_idx: dict[str, list[int]] = {}

    for eid, name, addr, count, *_ in entities:
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
    skipped_city_zip = 0
    skipped_large = 0
    for addr, eids in addr_idx.items():
        if _CITY_ZIP_ONLY.match(addr):
            skipped_city_zip += 1
            continue
        if len(eids) > BASE_MAX_ADDR_ENTITIES:
            skipped_large += 1
            continue
        if len(eids) > 1:
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="same_addr")
                    addr_edges += 1

    print(f"  {name_edges:,} name edges, {addr_edges:,} addr edges")
    print(f"  Skipped: {skipped_city_zip:,} city/zip-only, {skipped_large:,} large office addresses")
    print(f"  Graph: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")
    return G


def compute_base_clusters(G, entities):
    """Assign base cluster IDs and compute parcel counts per base cluster."""
    print("  Computing base clusters...")
    components = list(nx.connected_components(G))
    # entity_id → base_cluster_id
    base_cluster_of: dict[int, int] = {}
    for cid, component in enumerate(components):
        for eid in component:
            base_cluster_of[eid] = cid

    # base_cluster_id → total parcel count
    parcel_count_of: dict[int, int] = {}
    eid_to_count = {eid: count for eid, _, _, count, *_ in entities}
    for eid, cid in base_cluster_of.items():
        parcel_count_of[cid] = parcel_count_of.get(cid, 0) + eid_to_count.get(eid, 0)

    n_clusters = len(components)
    large = sum(1 for v in parcel_count_of.values() if v > MAX_MERGE_PARCELS)
    print(f"  {n_clusters:,} base clusters, {large:,} with >{MAX_MERGE_PARCELS} parcels (protected from SOS expansion)")
    return base_cluster_of, parcel_count_of


def can_merge(eid1, eid2, base_cluster_of, parcel_count_of):
    """True if both endpoints' base clusters are small enough to allow a SOS merge."""
    cid1 = base_cluster_of.get(eid1, -1)
    cid2 = base_cluster_of.get(eid2, -1)
    if cid1 == cid2:
        return True  # same base cluster — SOS edge just confirms existing link
    return (parcel_count_of.get(cid1, 0) <= MAX_MERGE_PARCELS and
            parcel_count_of.get(cid2, 0) <= MAX_MERGE_PARCELS)


# ---------------------------------------------------------------------------
# Pass 2: SOS edges (size-gated)
# ---------------------------------------------------------------------------

def add_ra_edges(G, entities, base_cluster_of, parcel_count_of):
    print(f"\nPass 2a: shared registered-agent edges...")
    ra_idx: dict[str, list[int]] = {}
    ra_names: dict[str, str] = {}

    for eid, name, addr, count, sos_cn, ra_id, ra_name, match_type in entities:
        if not ra_id or match_type not in ('exact', 'trgm_high'):
            continue
        key = ra_key(ra_name or "")
        if key in COMMERCIAL_RA_SKIP:
            continue
        ra_idx.setdefault(ra_id, []).append(eid)
        ra_names[ra_id] = ra_name or ""

    added = skipped_large = skipped_gate = 0
    for ra_id, eids in ra_idx.items():
        if len(eids) < 2:
            continue
        if len(eids) > MAX_RA_ENTITIES:
            skipped_large += 1
            continue
        for i in range(len(eids)):
            for j in range(i + 1, len(eids)):
                if not can_merge(eids[i], eids[j], base_cluster_of, parcel_count_of):
                    skipped_gate += 1
                    continue
                if not G.has_edge(eids[i], eids[j]):
                    G.add_edge(eids[i], eids[j], rel="shared_ra", ra=ra_names[ra_id])
                    added += 1

    print(f"  {added:,} edges added  |  {skipped_large:,} RAs too large  |  {skipped_gate:,} blocked by size gate")
    return added


def add_officer_edges(G, engine, entities, base_cluster_of, parcel_count_of):
    print(f"Pass 2b: shared officer edges...")

    enriched = {
        eid: cn
        for eid, name, addr, count, cn, ra_id, ra_name, match_type in entities
        if cn and match_type in ('exact', 'trgm_high')
    }
    if not enriched:
        print("  No enriched entities — skipping")
        return 0

    cns = list({cn for cn in enriched.values()})
    with engine.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE _enrich_cns (control_number TEXT) ON COMMIT DROP"))
        CHUNK = 5000
        for i in range(0, len(cns), CHUNK):
            conn.execute(text("INSERT INTO _enrich_cns VALUES (:cn)"),
                         [{"cn": cn} for cn in cns[i:i+CHUNK]])
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

    officer_cns: dict[tuple, set] = {}
    for cn, fn, ln in rows:
        if fn and ln and len(ln) > 1:
            officer_cns.setdefault((fn, ln), set()).add(cn)

    cn_to_eids: dict[str, list[int]] = {}
    for eid, cn in enriched.items():
        cn_to_eids.setdefault(cn, []).append(eid)

    added = skipped_large = skipped_gate = 0
    for (fn, ln), cns_for_officer in officer_cns.items():
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
                if not can_merge(eids_for_officer[i], eids_for_officer[j],
                                 base_cluster_of, parcel_count_of):
                    skipped_gate += 1
                    continue
                if not G.has_edge(eids_for_officer[i], eids_for_officer[j]):
                    G.add_edge(eids_for_officer[i], eids_for_officer[j],
                               rel="shared_officer", officer=f"{fn} {ln}")
                    added += 1

    print(f"  {added:,} edges added  |  {skipped_large:,} officers too large  |  {skipped_gate:,} blocked by size gate")
    return added


def add_sos_addr_edges(G, engine, entities, base_cluster_of, parcel_count_of):
    print(f"Pass 2c: shared SOS principal address edges...")

    enriched_cns = {
        eid: cn
        for eid, name, addr, count, cn, ra_id, ra_name, match_type in entities
        if cn and match_type in ('exact', 'trgm_high')
    }
    if not enriched_cns:
        return 0

    cns = list({cn for cn in enriched_cns.values()})
    with engine.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE _enrich_cns2 (control_number TEXT) ON COMMIT DROP"))
        CHUNK = 5000
        for i in range(0, len(cns), CHUNK):
            conn.execute(text("INSERT INTO _enrich_cns2 VALUES (:cn)"),
                         [{"cn": cn} for cn in cns[i:i+CHUNK]])
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

    addr_cns: dict[tuple, set] = {}
    for cn, street, city, state in rows:
        if street and city:
            addr_cns.setdefault((street, city, state or ''), set()).add(cn)

    cn_to_eids: dict[str, list[int]] = {}
    for eid, cn in enriched_cns.items():
        cn_to_eids.setdefault(cn, []).append(eid)

    added = skipped_large = skipped_gate = 0
    for (street, city, state), cns_for_addr in addr_cns.items():
        eids_for_addr = []
        for cn in cns_for_addr:
            eids_for_addr.extend(cn_to_eids.get(cn, []))
        if len(eids_for_addr) < 2:
            continue
        if len(eids_for_addr) > MAX_SOS_ADDR_ENTITIES:
            skipped_large += 1
            continue
        for i in range(len(eids_for_addr)):
            for j in range(i + 1, len(eids_for_addr)):
                if not can_merge(eids_for_addr[i], eids_for_addr[j],
                                 base_cluster_of, parcel_count_of):
                    skipped_gate += 1
                    continue
                if not G.has_edge(eids_for_addr[i], eids_for_addr[j]):
                    G.add_edge(eids_for_addr[i], eids_for_addr[j],
                               rel="shared_sos_addr",
                               addr=f"{street}, {city}")
                    added += 1

    print(f"  {added:,} edges added  |  {skipped_large:,} addresses too large  |  {skipped_gate:,} blocked by size gate")
    return added


# ---------------------------------------------------------------------------
# Re-cluster and write back
# ---------------------------------------------------------------------------

def reassign_clusters(engine, G):
    print("\nFinding connected components...")
    components = list(nx.connected_components(G))
    components.sort(key=len, reverse=True)
    print(f"  {len(components):,} clusters")

    cluster_map = {eid: cid for cid, comp in enumerate(components, 1) for eid in comp}

    print("Writing cluster assignments...")
    with engine.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE tmp_clusters (entity_id BIGINT, cluster_id INT)"))
        updates = [{"eid": eid, "cid": cid} for eid, cid in cluster_map.items()]
        CHUNK = 50000
        for i in range(0, len(updates), CHUNK):
            conn.execute(text("INSERT INTO tmp_clusters VALUES (:eid, :cid)"),
                         updates[i:i+CHUNK])
        conn.execute(text("""
            UPDATE owner_entities oe
            SET cluster_id = tc.cluster_id
            FROM tmp_clusters tc WHERE oe.entity_id = tc.entity_id
        """))
        conn.execute(text("DROP TABLE tmp_clusters"))

    print("Rebuilding ownership_clusters...")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ownership_clusters CASCADE"))
        conn.execute(text("""
            CREATE TABLE ownership_clusters AS
            SELECT
                cluster_id,
                COUNT(*)                                                     AS entity_count,
                SUM(count)                                                   AS parcel_count,
                ARRAY_AGG(DISTINCT owner_name_norm ORDER BY owner_name_norm) AS owner_names,
                ARRAY_AGG(DISTINCT owner_addr_norm ORDER BY owner_addr_norm)
                    FILTER (WHERE owner_addr_norm != '')                     AS owner_addresses,
                COUNT(DISTINCT sos_control_number)
                    FILTER (WHERE sos_control_number IS NOT NULL)            AS sos_entity_count,
                MODE() WITHIN GROUP (ORDER BY sos_status)                   AS primary_sos_status,
                MODE() WITHIN GROUP (ORDER BY sos_foreign_state)
                    FILTER (WHERE sos_foreign_state IS NOT NULL
                              AND sos_foreign_state <> '')                   AS primary_foreign_state,
                ARRAY_AGG(DISTINCT sos_registered_agent ORDER BY sos_registered_agent)
                    FILTER (WHERE sos_registered_agent IS NOT NULL
                              AND sos_registered_agent <> '')                AS registered_agents
            FROM owner_entities
            GROUP BY cluster_id
            ORDER BY parcel_count DESC
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oc_cluster ON ownership_clusters (cluster_id)"))

    return len(components)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(engine):
    with engine.connect() as conn:
        size_dist = conn.execute(text("""
            SELECT
              count(*) FILTER (WHERE parcel_count = 1)                AS single,
              count(*) FILTER (WHERE parcel_count BETWEEN 2 AND 5)    AS tiny,
              count(*) FILTER (WHERE parcel_count BETWEEN 6 AND 50)   AS small,
              count(*) FILTER (WHERE parcel_count BETWEEN 51 AND 500) AS medium,
              count(*) FILTER (WHERE parcel_count BETWEEN 501 AND 5000) AS large,
              count(*) FILTER (WHERE parcel_count > 5000)             AS mega
            FROM ownership_clusters
        """)).fetchone()

        top = conn.execute(text("""
            SELECT cluster_id, parcel_count, entity_count, sos_entity_count,
                   primary_sos_status, primary_foreign_state, owner_names[1:3]
            FROM ownership_clusters
            ORDER BY parcel_count DESC LIMIT 20
        """)).fetchall()

    print(f"\n--- Cluster size distribution ---")
    print(f"  single={size_dist[0]:,}  tiny={size_dist[1]:,}  small={size_dist[2]:,}  "
          f"medium={size_dist[3]:,}  large={size_dist[4]:,}  mega={size_dist[5]:,}")

    print(f"\nTop 20 clusters:")
    for r in top:
        names = (r.owner_names or [])[:3]
        state = f" [{r.primary_foreign_state}]" if r.primary_foreign_state else ""
        status = r.primary_sos_status or ""
        print(f"  {r.cluster_id:>6}: {r.parcel_count:>5} parcels, "
              f"{r.entity_count} entities, {r.sos_entity_count or 0} SOS"
              f"{state} {status} — {names}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    with engine.connect() as conn:
        n_orig = conn.execute(text("SELECT count(*) FROM ownership_clusters")).scalar()
    print(f"Starting cluster count: {n_orig:,}")

    entities = load_entities(engine)

    # Pass 1: base graph with tight cap
    base_G = build_base_graph(entities)
    base_cluster_of, parcel_count_of = compute_base_clusters(base_G, entities)

    # Pass 2: add SOS edges to the same graph (size-gated)
    ra_added      = add_ra_edges(base_G, entities, base_cluster_of, parcel_count_of)
    officer_added = add_officer_edges(base_G, engine, entities, base_cluster_of, parcel_count_of)
    addr_added    = add_sos_addr_edges(base_G, engine, entities, base_cluster_of, parcel_count_of)

    total_new = ra_added + officer_added + addr_added
    print(f"\nTotal SOS edges added: {total_new:,}")
    print(f"Final graph: {base_G.number_of_nodes():,} nodes, {base_G.number_of_edges():,} edges")

    n_after = reassign_clusters(engine, base_G)
    print(f"\n--- Cluster changes ---")
    print(f"  Before (script 04 original): 476,537")
    print(f"  After SOS enrichment:        {n_after:,}")
    print(f"  Net merges:                  {476537 - n_after:,}")
    print_stats(engine)
    print("\nDone.")


if __name__ == "__main__":
    main()
