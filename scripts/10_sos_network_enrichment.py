import re
import networkx as nx
from sqlalchemy import create_engine, text
from multiprocessing import Pool, cpu_count

from utils_clustering import (
    NAME_ENTROPY_LIMIT, INDIVIDUAL_NAME_ENTROPY_LIMIT, JUNK_NAME_BLOCKLIST,
    STREET_ENTITY_LIMIT, BUILDER_KEYWORDS, ADDRESS_STREET_BLOCKLIST,
    is_builder, normalize_street, is_commercial_ra, ra_key
)

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

# --- Tuning knobs ---
MAX_RA_ENTITIES        = 500  # skip RA if it manages this many of our entities
MAX_OFFICER_ENTITIES   = 50   # skip officer if appears this many times among our entities
MAX_SOS_ADDR_ENTITIES  = 100  # skip SOS address if this many entities share it

# SOS edge gate: skip if resulting merged cluster would be > this many parcels
# Increased to 10,000 now that institutional noise is removed.
MAX_MERGE_PARCELS      = 10000

_CITY_ZIP_ONLY = re.compile(r'^[A-Z]+(\s+[A-Z]+)*\s+[A-Z]{2}\s+\d{5}(-\d+)?$')

def load_entities(engine):
    print("Loading owner_entities...")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT entity_id, owner_name_norm, owner_addr_norm, count,
                   sos_control_number, sos_registered_agent_id,
                   sos_registered_agent, sos_match_type,
                   sos_registered_agent_address, is_institutional,
                   is_corporate, has_homestead
            FROM owner_entities
        """)).fetchall()
    return rows

def _get_name_edges(items):
    """Worker function for parallel name edge generation."""
    key, eids_with_flags = items
    edges = []
    if len(eids_with_flags) > 1:
        for i in range(len(eids_with_flags)):
            for j in range(i + 1, len(eids_with_flags)):
                eid1, hs1 = eids_with_flags[i]
                eid2, hs2 = eids_with_flags[j]
                edges.append((eid1, eid2))
    return edges

def build_base_graph(entities):
    print(f"\\nPass 1: base graph (STREET-level cap = {STREET_ENTITY_LIMIT})...")
    G = nx.Graph()
    name_idx = {}
    addr_idx = {}
    street_counts = {}
    eid_to_name = {}
    eid_to_corp = {}
    eid_to_addr = {}

    for row in entities:
        eid, name, addr, count = row[0], row[1], row[2], row[3]
        inst, corp, hs = row[9], row[10], row[11]
        G.add_node(eid)
        eid_to_name[eid] = name
        eid_to_corp[eid] = corp
        eid_to_addr[eid] = addr
        if inst: continue
        
        name_idx.setdefault(name, []).append((eid, hs))
        if addr:
            addr_idx.setdefault(addr, []).append(eid)
            street = normalize_street(addr)
            if street:
                street_counts[street] = street_counts.get(street, 0) + 1

    # 1. Name Edges (with Entropy Filter and Blocklist)
    print("  Calculating name entropy...")
    name_entropy = {}
    for name, eids_with_flags in name_idx.items():
        addrs = {eid_to_addr[eid] for eid, _ in eids_with_flags if eid_to_addr.get(eid)}
        name_entropy[name] = len(addrs)

    print("Filtering names by entropy and blocklist...")
    valid_name_items = []
    skipped_names_entropy = 0
    skipped_names_blocklist = 0
    skipped_names_homestead = 0
    
    for name, eids_with_flags in name_idx.items():
        if name in JUNK_NAME_BLOCKLIST or any(name.startswith(j + ' ') for j in JUNK_NAME_BLOCKLIST):
            skipped_names_blocklist += 1
            continue
            
        entropy = name_entropy.get(name, 0)
        is_corp_name = any(eid_to_corp.get(eid, False) for eid, _ in eids_with_flags)
        
        homestead_count = sum(1 for _, hs in eids_with_flags if hs)
        if not is_corp_name and homestead_count > 1:
            skipped_names_homestead += 1
            continue
            
        limit = NAME_ENTROPY_LIMIT if is_corp_name else INDIVIDUAL_NAME_ENTROPY_LIMIT
        if entropy > limit:
            skipped_names_entropy += 1
            continue
            
        valid_name_items.append((name, eids_with_flags))
    
    print(f"  Connecting by shared name (skipping {skipped_names_entropy:,} high-entropy, {skipped_names_blocklist:,} blocklisted, {skipped_names_homestead:,} multi-homestead)...")
    with Pool(cpu_count()) as pool:
        results = pool.map(_get_name_edges, valid_name_items)
        for chunk in results:
            G.add_edges_from(chunk, rel="same_name")

    # 2. Address Edges
    skipped_addr_builder = 0
    valid_addr_items = []
    for addr, eids in addr_idx.items():
        if _CITY_ZIP_ONLY.match(addr): continue
        street = normalize_street(addr)
        if any(street.startswith(b) for b in ADDRESS_STREET_BLOCKLIST): continue
        if street_counts.get(street, 0) > STREET_ENTITY_LIMIT: continue

        if any(is_builder(eid_to_name.get(eid, "")) for eid in eids) and len(eids) >= 5:
            skipped_addr_builder += 1
            continue

        valid_addr_items.append((addr, eids))

    for addr, eids in valid_addr_items:
        for i in range(len(eids)):
            for j in range(i + 1, len(eids)):
                G.add_edge(eids[i], eids[j], rel="same_addr")
    
    if skipped_addr_builder:
        print(f"  Skipped {skipped_addr_builder:,} builder-buyer address hubs")
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
        if is_commercial_ra(ra_name): continue
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
            SELECT o.control_number, upper(trim(o.first_name)), upper(trim(o.last_name)), upper(trim(o.description))
            FROM sos.officers o JOIN _enrich_cns ec ON ec.control_number = o.control_number
            WHERE o.first_name IS NOT NULL AND trim(o.first_name) <> ''
              AND o.last_name IS NOT NULL AND trim(o.last_name) <> ''
        """)).fetchall()

    off_idx = {}
    cn_to_eids = {}
    unique_officers = {} # (FN, LN) -> set(roles)
    
    for eid, cn in enriched.items(): cn_to_eids.setdefault(cn, []).append(eid)
    for cn, fn, ln, desc in rows:
        key = f"{fn} {ln}"
        if fn and ln and len(ln) > 1:
            off_idx.setdefault(key, []).extend(cn_to_eids.get(cn, []))
            unique_officers.setdefault((fn, ln), set()).add(desc)

    # Global Frequency Check for Organizers
    # We only care about officers that are in our dataset first
    officer_keys = list(unique_officers.keys())
    global_counts = {}
    if officer_keys:
        organizer_keys = [(fn, ln) for (fn, ln), roles in unique_officers.items()
                          if any(r in ('ORGANIZER', 'INCORPORATOR') for r in roles)]
        print(f"  Checking global SOS counts for {len(organizer_keys):,} organizer/incorporator officers "
              f"(of {len(officer_keys):,} total)...")
        if organizer_keys:
            with engine.begin() as conn:
                # Use precomputed sos.officer_global_counts if available (instant lookup).
                # Fall back to a single-query scan with the functional index otherwise.
                has_cache = conn.execute(text("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'sos' AND table_name = 'officer_global_counts'
                    )
                """)).scalar()
                conn.execute(text(
                    "CREATE TEMP TABLE _off_check (fn TEXT, ln TEXT) ON COMMIT DROP"
                ))
                for i in range(0, len(organizer_keys), 5000):
                    conn.execute(
                        text("INSERT INTO _off_check (fn, ln) VALUES (:fn, :ln)"),
                        [{"fn": fn, "ln": ln} for fn, ln in organizer_keys[i:i+5000]],
                    )
                if has_cache:
                    res = conn.execute(text("""
                        SELECT oc.fn, oc.ln, ogc.global_count
                        FROM _off_check oc
                        JOIN sos.officer_global_counts ogc USING (fn, ln)
                    """)).fetchall()
                else:
                    print("  (sos.officer_global_counts not found — falling back to live scan, slow)")
                    conn.execute(text("CREATE INDEX ON _off_check (fn, ln)"))
                    conn.execute(text("ANALYZE _off_check"))
                    res = conn.execute(text("""
                        SELECT oc.fn, oc.ln, COUNT(DISTINCT o.control_number)
                        FROM _off_check oc
                        JOIN sos.officers o
                          ON upper(trim(o.first_name)) = oc.fn
                         AND upper(trim(o.last_name))  = oc.ln
                        GROUP BY oc.fn, oc.ln
                    """)).fetchall()
                for fn, ln, count in res:
                    global_counts[f"{fn} {ln}"] = count

    valid_items = []
    for key, eids in off_idx.items():
        unique_eids = list(set(eids))
        if len(unique_eids) > MAX_OFFICER_ENTITIES: continue
        
        # Role + Global Frequency Filter
        fn, ln = key.split(' ', 1)
        roles = unique_officers.get((fn, ln), set())
        g_count = global_counts.get(key, 0)
        
        # If it's a professional organizer with > 500 companies globally, skip
        if any(r in ('ORGANIZER', 'INCORPORATOR') for r in roles) and g_count > 500:
            continue
            
        valid_items.append((key, unique_eids))
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
    street_counts = {}
    cn_to_eids = {}
    for eid, cn in enriched.items(): cn_to_eids.setdefault(cn, []).append(eid)
    
    for cn, street, unit, city, state in rows:
        key = f"{street} {unit} {city} {state}".strip()
        eids = cn_to_eids.get(cn, [])
        addr_idx.setdefault(key, []).extend(eids)
        
        # Street-level normalization for building hub detection
        norm_st = normalize_street(street)
        if norm_st:
            street_counts[norm_st] = street_counts.get(norm_st, 0) + len(eids)

    valid_items = []
    for key, eids in addr_idx.items():
        unique_eids = list(set(eids))
        if len(unique_eids) > MAX_SOS_ADDR_ENTITIES: continue
        
        # Check building-level hub
        street_part = key.split(' ')[0:2] # Crude but normalize_street is better
        # We already have street_counts from the 'street' variable in the loop
        # But we need to map the 'key' back to its normalized street
        # Let's re-extract it or store it.
        # Actually, let's just use the 'street' from the 'rows' again.
        
    # Redo the loop slightly more cleanly to keep track of street -> key mapping
    addr_idx = {}
    key_to_street = {}
    street_counts = {}
    for cn, street, unit, city, state in rows:
        key = f"{street} {unit} {city} {state}".strip()
        eids = cn_to_eids.get(cn, [])
        addr_idx.setdefault(key, []).extend(eids)
        
        norm_st = normalize_street(street)
        key_to_street[key] = norm_st
        if norm_st:
            street_counts[norm_st] = street_counts.get(norm_st, 0) + len(eids)

    valid_items = []
    for key, eids in addr_idx.items():
        unique_eids = list(set(eids))
        if len(unique_eids) > MAX_SOS_ADDR_ENTITIES: continue
        
        norm_st = key_to_street.get(key)
        if norm_st and street_counts.get(norm_st, 0) > STREET_ENTITY_LIMIT:
            continue
            
        valid_items.append((key, unique_eids))
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
