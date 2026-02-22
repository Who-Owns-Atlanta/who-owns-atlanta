
import re
import networkx as nx
from sqlalchemy import create_engine, text
from multiprocessing import Pool, cpu_count

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

# --- Tuning knobs ---
# Skip names with many distinct addresses (likely generic labels like 'BRANDYWINE')
NAME_ENTROPY_LIMIT = 20

# Skip addresses if shared by many entities (mailbox centers, office parks)
# We check this at the STREET level (ignoring Suite/Unit)
STREET_ENTITY_LIMIT = 50

# Skip city/zip-only addresses (PO Box artifacts from libpostal stripping box numbers)
CITY_ZIP_ONLY = re.compile(r'^[A-Z]+(\s+[A-Z]+)*\s+[A-Z]{2}\s+\d{5}(-\d+)?$')

def normalize_street(addr: str) -> str:
    """Strip Suite/Unit/Apt from address to find the base building."""
    if not addr: return ""
    return re.sub(r'\s+(STE|SUITE|UNIT|BLDG|OFFICE|#|APT)\s+.*$', '', addr, flags=re.IGNORECASE).strip()

def build_owner_entities(engine):
    """Create a table of distinct (owner_name_norm, owner_addr_norm) pairs."""
    print("Building owner entities...")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS owner_entities CASCADE;"))
        conn.execute(text("""
            CREATE TABLE owner_entities AS
            SELECT
                ROW_NUMBER() OVER () AS entity_id,
                owner_name_norm,
                owner_addr_norm,
                county,
                count,
                parcel_ids
            FROM (
                SELECT
                    UPPER(TRIM(owner_name)) AS owner_name_norm,
                    COALESCE(owner_addr_norm, '') AS owner_addr_norm,
                    county,
                    COUNT(*) AS count,
                    ARRAY_AGG(parcel_id) AS parcel_ids
                FROM parcels_unified
                WHERE owner_name IS NOT NULL AND TRIM(owner_name) != ''
                GROUP BY UPPER(TRIM(owner_name)), COALESCE(owner_addr_norm, ''), county
            ) sub;
        """))
        total = conn.execute(text("SELECT COUNT(*) FROM owner_entities")).scalar()
        print(f"  {total:,} distinct owner entities")
    return total

def _get_edges(items):
    """Worker function for parallel edge generation."""
    key, eids = items
    edges = []
    if len(eids) > 1:
        for i in range(len(eids)):
            for j in range(i + 1, len(eids)):
                edges.append((eids[i], eids[j]))
    return edges

def build_network(engine):
    """Build a networkx graph connecting entities by shared name or address."""
    print("Loading entities for graph construction...")
    with engine.connect() as conn:
        entities = conn.execute(text("""
            SELECT entity_id, owner_name_norm, owner_addr_norm
            FROM owner_entities
        """)).fetchall()

    print(f"  {len(entities):,} entities loaded")

    G = nx.Graph()
    name_idx = {}
    addr_idx = {}
    street_counts = {}

    for eid, name, addr in entities:
        G.add_node(eid)
        name_idx.setdefault(name, []).append(eid)
        if addr:
            addr_idx.setdefault(addr, []).append(eid)
            street = normalize_street(addr)
            street_counts[street] = street_counts.get(street, 0) + 1

    # 1. Name Edges (with Entropy Filter)
    print(f"Filtering names with entropy > {NAME_ENTROPY_LIMIT}...")
    valid_name_items = []
    skipped_names = 0
    for name, eids in name_idx.items():
        # Count distinct addresses for this name
        with engine.connect() as conn:
            # Note: This could be slow in a loop. Better to fetch entropy for all names at once.
            pass
    
    # Optimization: Pre-calculate entropy for all names
    print("  Calculating name entropy...")
    with engine.connect() as conn:
        entropy_rows = conn.execute(text("""
            SELECT owner_name_norm, COUNT(DISTINCT owner_addr_norm) 
            FROM owner_entities GROUP BY owner_name_norm
        """)).fetchall()
        name_entropy = {row[0]: row[1] for row in entropy_rows}

    for name, eids in name_idx.items():
        if name_entropy.get(name, 0) > NAME_ENTROPY_LIMIT:
            skipped_names += 1
            continue
        valid_name_items.append((name, eids))
    
    print(f"  Connecting by shared name (skipping {skipped_names:,} generic names)...")
    with Pool(cpu_count()) as pool:
        results = pool.map(_get_edges, valid_name_items)
        for chunk in results:
            G.add_edges_from(chunk, rel="same_name")
    
    # 2. Address Edges (with Street-Level Gating)
    print(f"Filtering addresses by street entropy (Limit: {STREET_ENTITY_LIMIT})...")
    valid_addr_items = []
    skipped_addr_cityzip = 0
    skipped_addr_hub = 0

    for addr, eids in addr_idx.items():
        if CITY_ZIP_ONLY.match(addr):
            skipped_addr_cityzip += 1
            continue
        
        street = normalize_street(addr)
        if street_counts.get(street, 0) > STREET_ENTITY_LIMIT:
            skipped_addr_hub += 1
            continue
            
        valid_addr_items.append((addr, eids))

    print(f"  Connecting by shared address (skipped {skipped_addr_cityzip:,} city/zip, {skipped_addr_hub:,} hubs)...")
    with Pool(cpu_count()) as pool:
        results = pool.map(_get_edges, valid_addr_items)
        for chunk in results:
            G.add_edges_from(chunk, rel="same_addr")

    print(f"  Graph: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")
    return G

def assign_clusters(engine, G):
    """Find connected components and assign cluster IDs."""
    print("Finding connected components...")
    components = list(nx.connected_components(G))
    print(f"  {len(components):,} clusters")
    components.sort(key=len, reverse=True)

    cluster_map = {}
    for cluster_id, component in enumerate(components, 1):
        for eid in component:
            cluster_map[eid] = cluster_id

    print("Writing cluster assignments...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE owner_entities ADD COLUMN IF NOT EXISTS cluster_id INT;"))
        conn.execute(text("CREATE TEMP TABLE tmp_clusters (entity_id BIGINT, cluster_id INT);"))
        updates = [{"eid": eid, "cid": cid} for eid, cid in cluster_map.items()]
        CHUNK = 50000
        for i in range(0, len(updates), CHUNK):
            conn.execute(text("INSERT INTO tmp_clusters (entity_id, cluster_id) VALUES (:eid, :cid)"), updates[i:i+CHUNK])
        conn.execute(text("UPDATE owner_entities oe SET cluster_id = tc.cluster_id FROM tmp_clusters tc WHERE oe.entity_id = tc.entity_id;"))
        conn.execute(text("DROP TABLE tmp_clusters;"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oe_cluster ON owner_entities (cluster_id);"))

    print("Rebuilding ownership_clusters...")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ownership_clusters CASCADE;"))
        conn.execute(text("""
            CREATE TABLE ownership_clusters AS
            WITH name_ranks AS (
                SELECT cluster_id, owner_name_norm, MAX(array_length(parcel_ids, 1)) AS max_pc
                FROM owner_entities GROUP BY cluster_id, owner_name_norm
            ),
            name_arrays AS (
                SELECT cluster_id, ARRAY_AGG(owner_name_norm ORDER BY max_pc DESC, owner_name_norm) AS owner_names
                FROM name_ranks GROUP BY cluster_id
            ),
            addr_arrays AS (
                SELECT cluster_id, ARRAY_AGG(DISTINCT owner_addr_norm ORDER BY owner_addr_norm) 
                FILTER (WHERE owner_addr_norm != '') AS owner_addresses
                FROM owner_entities GROUP BY cluster_id
            )
            SELECT oe.cluster_id, COUNT(*) AS entity_count, SUM(oe.count) AS parcel_count,
                   na.owner_names, aa.owner_addresses
            FROM owner_entities oe
            JOIN name_arrays na USING (cluster_id)
            JOIN addr_arrays aa USING (cluster_id)
            GROUP BY oe.cluster_id, na.owner_names, aa.owner_addresses
            ORDER BY parcel_count DESC;
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oc_cluster ON ownership_clusters (cluster_id);"))

    return len(components)

if __name__ == "__main__":
    build_owner_entities(engine)
    G = build_network(engine)
    assign_clusters(engine, G)
    print("\nNOTE: DROP TABLE owner_entities/ownership_clusters CASCADE was run above.")
    print("      mv_cluster_stats and mv_leaderboard have been dropped.")
    print("      After the full pipeline, recreate with:")
    print("        psql ... -f scripts/sql/04_create_materialized_views.sql")
    print("\nDone.")
