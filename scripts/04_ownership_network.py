import re
import networkx as nx
from sqlalchemy import create_engine, text
from multiprocessing import Pool, cpu_count

from utils_persistence import ensure_persistence_schema

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

# --- Tuning knobs ---
# Skip names with many distinct addresses (likely generic labels like 'BRANDYWINE')
# ... rest of file (import and engine part) ...
# Increased to 100 as institutional noise (MARTA, GA Power) is now institutional-flagged.
NAME_ENTROPY_LIMIT = 100

# Skip addresses if shared by many entities (mailbox centers, office parks)
# Lowered from 50 to 30 to prevent builder-to-buyer bridges while keeping legitimate operators
STREET_ENTITY_LIMIT = 30

# Known corporate developer keywords to trigger the builder-buyer heuristic
BUILDER_KEYWORDS = {'HORTON', 'BROCK', 'PULTE', 'LENNAR', 'CENTURY', 'BEAZER', 'ASHTON', 'MERITAGE', 'TOLL', 'KB HOME'}

# Addresses (matched as prefix of normalize_street output) that must never create
# address edges — multi-firm hub offices where unrelated institutional landlords
# happen to share a mailing address.
# Format: use the street number + name only (no suite, city, state, zip).
ADDRESS_STREET_BLOCKLIST = {
    '3505 KOGER BLVD',     # Duluth GA — Pretium (FYR SFR BORROWER) + Amherst (HOME SFR BORROWER)
    '5100 TAMARIND REEF',  # Christiansted USVI — same two firms share a USVI trust address
}

# Skip city/zip-only addresses (PO Box artifacts from libpostal stripping box numbers)
CITY_ZIP_ONLY = re.compile(r'^[A-Z]+(\s+[A-Z]+)*\s+[A-Z]{2}\s+\d{5}(-\d+)?$')

def is_builder(name: str) -> bool:
    """Check if owner name contains known builder keywords."""
    if not name: return False
    n = name.upper()
    return any(k in n for k in BUILDER_KEYWORDS)

def normalize_street(addr: str) -> str:
    """Strip Suite/Unit/Apt from address to find the base building."""
    if not addr: return ""
    # Remove junk characters
    s = re.sub(r'[.,?]', '', addr).strip()
    if not s or len(s) < 2: return ""
    # Strip suite/unit
    return re.sub(r'\s+(STE|SUITE|UNIT|BLDG|OFFICE|#|APT)\s+.*$', '', s, flags=re.IGNORECASE).strip()

def is_junk_addr(addr: str) -> bool:
    """Check if address is clearly a normalization artifact."""
    if not addr: return True
    # If it's just dots, hashes, or very short numbers
    if re.match(r'^[.#\s?0-9]+$', addr) and len(addr.strip()) < 8:
        return True
    return False

def build_owner_entities(engine):
    """Create a table of distinct (owner_name_norm, owner_addr_norm) pairs."""
    print("Building owner entities...")
    ensure_persistence_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS owner_entities CASCADE;"))

        # Build raw entity aggregates into a temp table
        conn.execute(text("""
            CREATE TEMP TABLE tmp_raw_entities AS
            SELECT
                CASE
                  WHEN UPPER(TRIM(owner_name)) LIKE '% & %'
                  THEN (
                    SELECT STRING_AGG(part, ' & ' ORDER BY part)
                    FROM UNNEST(STRING_TO_ARRAY(UPPER(TRIM(owner_name)), ' & ')) AS part
                  )
                  ELSE UPPER(TRIM(owner_name))
                END AS owner_name_norm,
                COALESCE(owner_addr_norm, '') AS owner_addr_norm,
                county,
                BOOL_OR(is_institutional) AS is_institutional,
                BOOL_OR(is_corporate) AS is_corporate,
                COUNT(*) AS count,
                ARRAY_AGG(parcel_id) AS parcel_ids
            FROM parcels_unified
            WHERE owner_name IS NOT NULL AND TRIM(owner_name) != ''
            GROUP BY
                CASE
                  WHEN UPPER(TRIM(owner_name)) LIKE '% & %'
                  THEN (
                    SELECT STRING_AGG(part, ' & ' ORDER BY part)
                    FROM UNNEST(STRING_TO_ARRAY(UPPER(TRIM(owner_name)), ' & ')) AS part
                  )
                  ELSE UPPER(TRIM(owner_name))
                END,
                COALESCE(owner_addr_norm, ''), county;
        """))

        # Upsert into entity_registry to assign/retrieve stable entity_ids
        conn.execute(text("""
            INSERT INTO entity_registry (name_norm, addr_norm, county)
            SELECT owner_name_norm, owner_addr_norm, county
            FROM tmp_raw_entities
            ON CONFLICT (name_norm, addr_norm, county) DO UPDATE
                SET last_seen = NOW();
        """))

        # Create owner_entities with stable entity_id from registry
        conn.execute(text("""
            CREATE TABLE owner_entities AS
            SELECT
                er.entity_id,
                ne.owner_name_norm,
                ne.owner_addr_norm,
                ne.county,
                ne.is_institutional,
                ne.is_corporate,
                ne.count,
                ne.parcel_ids
            FROM tmp_raw_entities ne
            JOIN entity_registry er
                ON er.name_norm = ne.owner_name_norm
               AND er.addr_norm = ne.owner_addr_norm
               AND er.county    = ne.county;
        """))

        conn.execute(text("DROP TABLE tmp_raw_entities;"))

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
            SELECT entity_id, owner_name_norm, owner_addr_norm, is_institutional
            FROM owner_entities
        """)).fetchall()

    print(f"  {len(entities):,} entities loaded")

    G = nx.Graph()
    name_idx = {}
    addr_idx = {}
    street_counts = {}
    is_inst = {}
    eid_to_name = {}

    for eid, name, addr, inst in entities:
        G.add_node(eid)
        is_inst[eid] = inst
        eid_to_name[eid] = name
        if inst: continue # Skip indexing for institutional bridges
        
        name_idx.setdefault(name, []).append(eid)
        if addr:
            addr_idx.setdefault(addr, []).append(eid)
            street = normalize_street(addr)
            if street:
                street_counts[street] = street_counts.get(street, 0) + 1

    # 1. Name Edges (with Entropy Filter)
    print(f"Filtering names with entropy > {NAME_ENTROPY_LIMIT}...")
    valid_name_items = []
    skipped_names = 0
    
    print("  Calculating name entropy...")
    with engine.connect() as conn:
        entropy_rows = conn.execute(text("""
            SELECT owner_name_norm, COUNT(DISTINCT owner_addr_norm) 
            FROM owner_entities 
            WHERE NOT is_institutional
            GROUP BY owner_name_norm
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
    
    # 2. Address Edges (with Street-Level Gating and Builder-Buyer Heuristic)
    print(f"Filtering addresses by street entropy (Limit: {STREET_ENTITY_LIMIT})...")
    valid_addr_items = []
    skipped_addr_cityzip = 0
    skipped_addr_hub = 0
    skipped_addr_junk = 0
    skipped_addr_builder = 0

    for addr, eids in addr_idx.items():
        if CITY_ZIP_ONLY.match(addr):
            skipped_addr_cityzip += 1
            continue
        
        if is_junk_addr(addr):
            skipped_addr_junk += 1
            continue

        street = normalize_street(addr)
        if any(street.upper().startswith(b) for b in ADDRESS_STREET_BLOCKLIST):
            skipped_addr_hub += 1
            continue

        if street_counts.get(street, 0) > STREET_ENTITY_LIMIT:
            skipped_addr_hub += 1
            continue

        # Builder-Buyer Heuristic:
        # If an address contains a known builder and 5+ other entities, it's likely a residential development hub.
        if any(is_builder(eid_to_name.get(eid)) for eid in eids) and len(eids) >= 5:
            skipped_addr_builder += 1
            continue

        valid_addr_items.append((addr, eids))

    print(f"  Connecting by shared address (skipped {skipped_addr_cityzip:,} city/zip, {skipped_addr_hub:,} hubs, {skipped_addr_junk:,} junk, {skipped_addr_builder:,} builder hubs)...")
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
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oe_cluster_county ON owner_entities (cluster_id, county);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oe_parcel_ids_gin ON owner_entities USING GIN (parcel_ids);"))

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
    print("\nDone.")
