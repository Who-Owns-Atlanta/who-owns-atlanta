import re
import networkx as nx
from sqlalchemy import create_engine, text
from multiprocessing import Pool, cpu_count

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

# --- Tuning knobs ---
MAX_RA_ENTITIES        = 500  # skip RA if it manages this many of our entities
MAX_OFFICER_ENTITIES   = 50   # skip officer if appears this many times among our entities
MAX_SOS_ADDR_ENTITIES  = 100  # skip SOS address if this many entities share it

# SOS edge gate: skip if resulting merged cluster would be > this many parcels
# Increased to 10,000 now that institutional noise is removed.
MAX_MERGE_PARCELS      = 10000

# Skip addresses (Pass 1 & Pass 2) if shared by many entities at the street level
STREET_ENTITY_LIMIT    = 50

# Expanded Professional Blacklist
COMMERCIAL_RA_SKIP = {
    "CORPORATION SERVICE COMPANY", "C T CORPORATION SYSTEM", "CT CORPORATION SYSTEM",
    "COGENCY GLOBAL INC", "NORTHWEST REGISTERED AGENT SERVICE INC", "NORTHWEST REGISTERED AGENT LLC",
    "REGISTERED AGENTS INC", "NATIONAL REGISTERED AGENTS INC", "UNITED STATES CORPORATION AGENTS INC",
    "CORPORATE CREATIONS NETWORK INC", "CSC OF COBB COUNTY INC", "VCORP AGENT SERVICES INC",
    "INCORP SERVICES INC", "ANDERSON REGISTERED AGENTS INC", "REPUBLIC REGISTERED AGENT LLC",
    "ACCESS MANAGEMENT GROUP", "LEGALINC CORPORATE SERVICES INC", "PARACORP INC", "PARACORP INCORPORATED",
    "HOMEOWNER MANAGEMENT SERVICES INC", "HOMEOWNER MANAGEMENT SERVICES INC.",
    "COMMUNITY MANAGEMENT ASSOCIATES INC", "COMMUNITY MANAGEMENT ASSOCIATES INC.",
    "COMMUNITY MANAGEMENT ASSOCIATES, INC.", "FIELDSTONE REALTY PARTNERS LLC", "FIELDSTONE REALTY PARTNERS, LLC",
    "SENTRY MANAGEMENT INC", "SENTRY MANAGEMENT INC.", "HOMESIDE PROPERTIES", "HOMESIDE PROPERTIES, INC",
    "HOMESIDE PROPERTIES, INC.", "SILVERLEAF MANAGEMENT GROUP LLC", "SILVERLEAF MANAGEMENT GROUP, LLC",
    "GEORGIA REGISTERED AGENT LLC", "GEORGIA REGISTERED AGENT", "BUSINESS FILINGS INCORPORATED",
    "UNIVERSAL REGISTERED AGENTS INC", "UNIVERSAL REGISTERED AGENTS, INC.",
    "BCS CORPORATE SERVICES INC", "BCS CORPORATE SERVICES, INC.",
    "TERRAPIN CORPORATE SERVICES LLC", "TERRAPIN CORPORATE SERVICES, LLC",
    "HERITAGE PROPERTY MANAGEMENT SERVICES LLC", "HERITAGE PROPERTY MANAGEMENT SERVICES, LLC",
    "ATLANTA COMMUNITY SERVICES INC", "ATLANTA COMMUNITY SERVICES, INC.",
    "BEACON COMMUNITY MANAGEMENT SERVICES LLC", "BEACON COMMUNITY MANAGEMENT SERVICES, LLC",
    "BEACON MANAGEMENT SERVICES", "TOLLEY COMMUNITY MANAGEMENT", "POSOLUTIONS INC", "POSOLUTIONS, INC",
    "CANOPY SERVICES INC", "CANOPY SERVICES, INC.", "SPI AGENT SOLUTIONS INC", "SPI AGENT SOLUTIONS, INC.",
    "PMI NORTHEAST ATLANTA", "LEE MASON", "NONE", "", "BILL WETTER", "SENTRY MANAGEMENT",
    "ZENBUSINESS INC", "REGISTERED AGENT SOLUTIONS INC", "REGISTERED AGENT SOLUTIONS, INC.",
    "CSC OF COBB COUNTY, INC.", "NORTHWEST REGISTERED AGENT SERVICE, INC.",
}

_STRIP_PUNCT = re.compile(r'[^A-Z0-9 ]')
_CITY_ZIP_ONLY = re.compile(r'^[A-Z]+(\s+[A-Z]+)*\s+[A-Z]{2}\s+\d{5}(-\d+)?$')

def normalize_street(addr: str) -> str:
    """Strip Suite/Unit/Apt from address to find the base building."""
    if not addr: return ""
    return re.sub(r'\s+(STE|SUITE|UNIT|BLDG|OFFICE|#|APT)\s+.*$', '', addr, flags=re.IGNORECASE).strip()

def ra_key(name: str, street: str = "") -> str:
    if not name: return ""
    name_part = _STRIP_PUNCT.sub("", name.upper()).strip()
    street_part = _STRIP_PUNCT.sub("", (street or "").upper()).strip()
    street_part = re.sub(r'\b(STE|SUITE|UNIT|BLDG|OFFICE|#)\s+.*$', '', street_part, flags=re.IGNORECASE).strip()
    return f"{name_part}|{street_part}"

def load_entities(engine):
    print("Loading owner_entities...")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT entity_id, owner_name_norm, owner_addr_norm, count,
                   sos_control_number, sos_registered_agent_id,
                   sos_registered_agent, sos_match_type,
                   sos_registered_agent_address, is_institutional
            FROM owner_entities
        """)).fetchall()
    return rows

def build_base_graph(entities):
    print(f"\nPass 1: base graph (STREET-level cap = {STREET_ENTITY_LIMIT})...")
    G = nx.Graph()
    name_idx = {}
    addr_idx = {}
    street_counts = {}

    for row in entities:
        eid, name, addr, count = row[0], row[1], row[2], row[3]
        inst = row[9]
        G.add_node(eid)
        if inst: continue
        
        name_idx.setdefault(name, []).append(eid)
        if addr:
            addr_idx.setdefault(addr, []).append(eid)
            street = normalize_street(addr)
            street_counts[street] = street_counts.get(street, 0) + 1

    for name, eids in name_idx.items():
        if len(eids) > 1:
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="same_name")

    for addr, eids in addr_idx.items():
        if _CITY_ZIP_ONLY.match(addr): continue
        street = normalize_street(addr)
        if street_counts.get(street, 0) > STREET_ENTITY_LIMIT: continue
        if len(eids) > 1:
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="same_addr")
    return G

def compute_base_clusters(G, entities):
    components = list(nx.connected_components(G))
    base_cluster_of = {}
    for cid, component in enumerate(components):
        for eid in component: base_cluster_of[eid] = cid
    
    parcel_count_of = {}
    eid_to_count = {eid: count for eid, _, _, count, *_ in entities}
    for eid, cid in base_cluster_of.items():
        parcel_count_of[cid] = parcel_count_of.get(cid, 0) + eid_to_count.get(eid, 0)
    return base_cluster_of, parcel_count_of

def can_merge(eid1, eid2, base_cluster_of, parcel_count_of):
    cid1, cid2 = base_cluster_of.get(eid1, -1), base_cluster_of.get(eid2, -1)
    if cid1 == cid2: return True
    return (parcel_count_of.get(cid1, 0) + parcel_count_of.get(cid2, 0)) <= MAX_MERGE_PARCELS

def _get_sos_edges(args):
    """Parallel worker to check merge constraints for SOS edges."""
    idx_items, base_cluster_of, parcel_count_of = args
    edges = []
    for key, eids in idx_items:
        if len(eids) < 2: continue
        for i in range(len(eids)):
            for j in range(i + 1, len(eids)):
                if can_merge(eids[i], eids[j], base_cluster_of, parcel_count_of):
                    edges.append((eids[i], eids[j], key))
    return edges

def add_ra_edges(G, entities, base_cluster_of, parcel_count_of):
    print(f"\nPass 2a: shared registered-agent edges...")
    ra_idx = {}
    for row in entities:
        eid, ra_name, match_type, ra_street = row[0], row[6], row[7], row[8]
        inst = row[9]
        if inst: continue
        if not ra_name or match_type not in ('exact', 'trgm_high'): continue
        name_only = _STRIP_PUNCT.sub("", ra_name.upper()).strip()
        if name_only in COMMERCIAL_RA_SKIP: continue
        key = ra_key(ra_name, ra_street)
        ra_idx.setdefault(key, []).append(eid)

    valid_items = [(k, v) for k, v in ra_idx.items() if len(v) <= MAX_RA_ENTITIES]
    added = 0
    with Pool(cpu_count()) as pool:
        results = pool.map(_get_sos_edges, [(valid_items[i:i + 500], base_cluster_of, parcel_count_of) for i in range(0, len(valid_items), 500)])
        for chunk in results:
            for u, v, label in chunk:
                if not G.has_edge(u, v):
                    G.add_edge(u, v, rel="shared_ra", label=label)
                    added += 1
    print(f"  {added:,} RA edges added")
    return added

def add_officer_edges(G, engine, entities, base_cluster_of, parcel_count_of):
    print(f"Pass 2b: shared officer edges...")
    enriched = {row[0]: row[4] for row in entities if row[4] and row[7] in ('exact', 'trgm_high') and not row[9]}
    if not enriched: return 0
    cns = list({cn for cn in enriched.values()})
    with engine.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE _enrich_cns (control_number TEXT) ON COMMIT DROP"))
        for i in range(0, len(cns), 5000):
            conn.execute(text("INSERT INTO _enrich_cns VALUES (:cn)"), [{"cn": cn} for cn in cns[i:i+5000]])
        rows = conn.execute(text("""
            SELECT o.control_number, upper(trim(o.first_name)), upper(trim(o.last_name))
            FROM sos.officers o JOIN _enrich_cns ec ON ec.control_number = o.control_number
            WHERE o.first_name IS NOT NULL AND trim(o.first_name) <> ''
              AND o.last_name IS NOT NULL AND trim(o.last_name) <> ''
        """)).fetchall()

    off_idx = {}
    cn_to_eids = {}
    for eid, cn in enriched.items(): cn_to_eids.setdefault(cn, []).append(eid)
    for cn, fn, ln in rows:
        if fn and ln and len(ln) > 1:
            off_idx.setdefault(f"{fn} {ln}", []).extend(cn_to_eids.get(cn, []))

    valid_items = [(k, list(set(v))) for k, v in off_idx.items() if len(set(v)) <= MAX_OFFICER_ENTITIES]
    added = 0
    with Pool(cpu_count()) as pool:
        results = pool.map(_get_sos_edges, [(valid_items[i:i+500], base_cluster_of, parcel_count_of) for i in range(0, len(valid_items), 500)])
        for chunk in results:
            for u, v, label in chunk:
                if not G.has_edge(u, v):
                    G.add_edge(u, v, rel="shared_officer", label=label)
                    added += 1
    print(f"  {added:,} Officer edges added")
    return added

def add_sos_addr_edges(G, engine, entities, base_cluster_of, parcel_count_of):
    print(f"Pass 2c: shared SOS principal address edges...")
    enriched = {row[0]: row[4] for row in entities if row[4] and row[7] in ('exact', 'trgm_high') and not row[9]}
    if not enriched: return 0
    cns = list({cn for cn in enriched.values()})
    with engine.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE _enrich_cns2 (control_number TEXT) ON COMMIT DROP"))
        for i in range(0, len(cns), 5000):
            conn.execute(text("INSERT INTO _enrich_cns2 VALUES (:cn)"), [{"cn": cn} for cn in cns[i:i+5000]])
        rows = conn.execute(text("""
            SELECT a.control_number, upper(trim(a.street_address1)), upper(trim(coalesce(a.street_address2,''))),
                   upper(trim(a.city)), upper(trim(a.state))
            FROM sos.addresses a JOIN _enrich_cns2 ec ON ec.control_number = a.control_number
            WHERE a.street_address1 IS NOT NULL AND trim(a.street_address1) <> ''
        """)).fetchall()

    addr_idx = {}
    cn_to_eids = {}
    for eid, cn in enriched.items(): cn_to_eids.setdefault(cn, []).append(eid)
    for cn, street, unit, city, state in rows:
        key = f"{street} {unit} {city} {state}".strip()
        addr_idx.setdefault(key, []).extend(cn_to_eids.get(cn, []))

    valid_items = [(k, list(set(v))) for k, v in addr_idx.items() if len(set(v)) <= MAX_SOS_ADDR_ENTITIES]
    added = 0
    with Pool(cpu_count()) as pool:
        results = pool.map(_get_sos_edges, [(valid_items[i:i+500], base_cluster_of, parcel_count_of) for i in range(0, len(valid_items), 500)])
        for chunk in results:
            for u, v, label in chunk:
                if not G.has_edge(u, v):
                    G.add_edge(u, v, rel="shared_sos_addr", label=label)
                    added += 1
    print(f"  {added:,} SOS Addr edges added")
    return added

def reassign_clusters(engine, G):
    print("\nFinding connected components...")
    components = list(nx.connected_components(G))
    components.sort(key=len, reverse=True)
    cluster_map = {eid: cid for cid, comp in enumerate(components, 1) for eid in comp}

    with engine.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE tmp_clusters (entity_id BIGINT, cluster_id INT)"))
        updates = [{"eid": eid, "cid": cid} for eid, cid in cluster_map.items()]
        for i in range(0, len(updates), 50000):
            conn.execute(text("INSERT INTO tmp_clusters VALUES (:eid, :cid)"), updates[i:i+50000])
        conn.execute(text("UPDATE owner_entities oe SET cluster_id = tc.cluster_id FROM tmp_clusters tc WHERE oe.entity_id = tc.entity_id"))
        conn.execute(text("DROP TABLE IF EXISTS ownership_clusters CASCADE"))
        conn.execute(text("""
            CREATE TABLE ownership_clusters AS
            WITH name_ranks AS (
                SELECT cluster_id, owner_name_norm, MAX(array_length(parcel_ids, 1)) AS max_pc
                FROM owner_entities GROUP BY cluster_id, owner_name_norm
            ),
            name_arrays AS (
                SELECT cluster_id, ARRAY_AGG(owner_name_norm ORDER BY max_pc DESC, owner_name_norm) AS owner_names
                FROM name_ranks GROUP BY cluster_id
            )
            SELECT oe.cluster_id, COUNT(*) AS entity_count, SUM(oe.count) AS parcel_count, na.owner_names,
                   ARRAY_AGG(DISTINCT oe.owner_addr_norm ORDER BY oe.owner_addr_norm) FILTER (WHERE oe.owner_addr_norm != '') AS owner_addresses,
                   COUNT(DISTINCT oe.sos_control_number) FILTER (WHERE oe.sos_control_number IS NOT NULL) AS sos_entity_count,
                   MODE() WITHIN GROUP (ORDER BY oe.sos_status) AS primary_sos_status
            FROM owner_entities oe JOIN name_arrays na USING (cluster_id)
            GROUP BY oe.cluster_id, na.owner_names ORDER BY parcel_count DESC
        """))
    return len(components)

if __name__ == "__main__":
    entities = load_entities(engine)
    G = build_base_graph(entities)
    base_cluster_of, parcel_count_of = compute_base_clusters(G, entities)
    add_ra_edges(G, entities, base_cluster_of, parcel_count_of)
    add_officer_edges(G, engine, entities, base_cluster_of, parcel_count_of)
    add_sos_addr_edges(G, engine, entities, base_cluster_of, parcel_count_of)
    reassign_clusters(engine, G)
    print("\nDone.")
    print("\nNOTE: DROP TABLE ownership_clusters CASCADE was run above.")
    print("      mv_cluster_stats and mv_leaderboard have been dropped.")
    print("      Recreate them:")
    print("        psql ... -f scripts/sql/04_create_materialized_views.sql")
