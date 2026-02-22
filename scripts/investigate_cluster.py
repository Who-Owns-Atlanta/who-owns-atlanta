
import re
import networkx as nx
from sqlalchemy import create_engine, text
import pandas as pd
from networkx.algorithms.community import louvain_communities

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

BASE_MAX_ADDR_ENTITIES = 10
MAX_RA_ENTITIES        = 100
MAX_OFFICER_ENTITIES   = 10
MAX_SOS_ADDR_ENTITIES  = 20
MAX_MERGE_PARCELS      = 200

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

def ra_key(name: str, street: str = "") -> str:
    if not name: return ""
    name_part = _STRIP_PUNCT.sub("", name.upper()).strip()
    street_part = _STRIP_PUNCT.sub("", (street or "").upper()).strip()
    street_part = re.sub(r'\b(STE|SUITE|UNIT|BLDG|OFFICE|#)\s+.*$', '', street_part).strip()
    return f"{name_part}|{street_part}"

def investigate(target_cluster_id):
    print(f"Investigating Cluster {target_cluster_id}...")
    
    with engine.connect() as conn:
        entities = conn.execute(text("""
            SELECT entity_id, owner_name_norm, owner_addr_norm, count,
                   sos_control_number, sos_registered_agent_id,
                   sos_registered_agent, sos_match_type,
                   sos_registered_agent_address
            FROM owner_entities
            WHERE cluster_id = :cid
        """), {"cid": target_cluster_id}).fetchall()
    
    print(f"  {len(entities):,} entities in cluster")
    
    # Reconstruct Base Graph for these entities
    G = nx.Graph()
    name_idx = {}
    addr_idx = {}
    eid_to_data = {}
    
    for row in entities:
        eid, name, addr, count = row[0], row[1], row[2], row[3]
        G.add_node(eid, name=name, addr=addr, count=count)
        name_idx.setdefault(name, []).append(eid)
        if addr:
            addr_idx.setdefault(addr, []).append(eid)
        eid_to_data[eid] = row

    # Base Edges (Name)
    for name, eids in name_idx.items():
        if len(eids) > 1:
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="same_name", label=name)

    # Base Edges (Addr)
    with engine.connect() as conn:
        addr_counts = conn.execute(text("""
            SELECT owner_addr_norm, COUNT(*) 
            FROM owner_entities 
            WHERE owner_addr_norm IN (
                SELECT DISTINCT owner_addr_norm FROM owner_entities WHERE cluster_id = :cid
            )
            GROUP BY owner_addr_norm
        """), {"cid": target_cluster_id}).fetchall()
        global_addr_counts = {row[0]: row[1] for row in addr_counts}

    for addr, eids in addr_idx.items():
        if _CITY_ZIP_ONLY.match(addr): continue
        if global_addr_counts.get(addr, 0) > BASE_MAX_ADDR_ENTITIES: continue
        if len(eids) > 1:
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="same_addr", label=addr)

    # Compute base clusters within this set (approximation)
    base_components = list(nx.connected_components(G))
    base_cluster_of = {}
    parcel_count_of = {}
    for i, comp in enumerate(base_components):
        total_p = 0
        for eid in comp:
            base_cluster_of[eid] = i
            total_p += eid_to_data[eid][3]
        parcel_count_of[i] = total_p

    def can_merge(eid1, eid2):
        cid1, cid2 = base_cluster_of[eid1], base_cluster_of[eid2]
        if cid1 == cid2: return True
        return parcel_count_of[cid1] <= MAX_MERGE_PARCELS and parcel_count_of[cid2] <= MAX_MERGE_PARCELS

    # SOS Edges
    # 1. RA
    ra_idx = {}
    for row in entities:
        eid, ra_name, match_type, ra_street = row[0], row[6], row[7], row[8]
        if not ra_name or match_type not in ('exact', 'trgm_high'): continue
        if _STRIP_PUNCT.sub("", ra_name.upper()).strip() in COMMERCIAL_RA_SKIP: continue
        key = ra_key(ra_name, ra_street)
        ra_idx.setdefault(key, []).append(eid)
    
    for key, eids in ra_idx.items():
        if len(eids) > MAX_RA_ENTITIES: continue
        for i in range(len(eids)):
            for j in range(i + 1, len(eids)):
                if can_merge(eids[i], eids[j]):
                    if not G.has_edge(eids[i], eids[j]):
                        G.add_edge(eids[i], eids[j], rel="shared_ra", label=key)

    # 2. Officer & SOS Addr (need to fetch from DB)
    cns = [row[4] for row in entities if row[4] and row[7] in ('exact', 'trgm_high')]
    if cns:
        with engine.connect() as conn:
            # Officers
            off_rows = conn.execute(text("""
                SELECT control_number, upper(trim(first_name)) as fn, upper(trim(last_name)) as ln
                FROM sos.officers WHERE control_number = ANY(:cns)
            """), {"cns": cns}).fetchall()
            
            # SOS Addr
            addr_rows = conn.execute(text("""
                SELECT control_number, upper(trim(street_address1)) as street, upper(trim(coalesce(street_address2,''))) as unit,
                       upper(trim(city)) as city, upper(trim(state)) as state
                FROM sos.addresses WHERE control_number = ANY(:cns)
            """), {"cns": cns}).fetchall()

        cn_to_eids = {}
        for row in entities:
            if row[4]: cn_to_eids.setdefault(row[4], []).append(row[0])

        # Officer Edges
        off_idx = {}
        for cn, fn, ln in off_rows:
            if fn and ln and len(ln) > 1:
                off_idx.setdefault((fn, ln), []).extend(cn_to_eids.get(cn, []))
        
        for key, eids in off_idx.items():
            if len(eids) > MAX_OFFICER_ENTITIES: continue
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    if can_merge(eids[i], eids[j]):
                        if not G.has_edge(eids[i], eids[j]):
                            G.add_edge(eids[i], eids[j], rel="shared_officer", label=f"{key[0]} {key[1]}")

        # SOS Addr Edges
        sos_addr_idx = {}
        for cn, street, unit, city, state in addr_rows:
            if street and city:
                sos_addr_idx.setdefault((street, unit, city, state or ''), []).extend(cn_to_eids.get(cn, []))
        
        for key, eids in sos_addr_idx.items():
            if len(eids) > MAX_SOS_ADDR_ENTITIES: continue
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    if can_merge(eids[i], eids[j]):
                        if not G.has_edge(eids[i], eids[j]):
                            G.add_edge(eids[i], eids[j], rel="shared_sos_addr", label=f"{key[0]} {key[1]}, {key[2]}")

    print(f"Graph reconstructed: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    
    # Community detection
    communities = list(louvain_communities(G))
    partition = {node: i for i, comm in enumerate(communities) for node in comm}
    
    print(f"\nLouvain Communities: {len(communities)}")
    comm_stats = []
    for comm_id, nodes in enumerate(communities):
        total_p = sum(G.nodes[n]['count'] for n in nodes)
        names = [G.nodes[n]['name'] for n in nodes]
        comm_stats.append({
            'comm_id': comm_id,
            'node_count': len(nodes),
            'parcel_count': total_p,
            'top_names': sorted(list(set(names)))[:3]
        })
    
    df_comm = pd.DataFrame(comm_stats).sort_values('parcel_count', ascending=False)
    print("\nTop Communities:")
    print(df_comm.head(10))

    # Bridges
    bridges = list(nx.bridges(G))
    print(f"\nFound {len(bridges)} bridges")
    
    # Articulation Points linking different communities
    print("\nArticulation Points linking different communities:")
    articulations = list(nx.articulation_points(G))
    inter_comm_arts = []
    for art in articulations:
        name = G.nodes[art]['name']
        neighbors = list(G.neighbors(art))
        neighbor_comms = {partition[v] for v in neighbors}
        if len(neighbor_comms) > 1:
            inter_comm_arts.append({
                'eid': art, 'name': name, 
                'comms': neighbor_comms,
                'degree': len(neighbors)
            })
    
    if inter_comm_arts:
        df_inter_art = pd.DataFrame(inter_comm_arts).sort_values('degree', ascending=False)
        print(df_inter_art.head(20))

        if not df_inter_art.empty:
            top_inter_art = df_inter_art.iloc[0]
            print(f"\nEdges for inter-community articulation point '{top_inter_art['name']}' (EID {top_inter_art['eid']}):")
            u = top_inter_art['eid']
            comms = sorted(list(top_inter_art['comms']))
            for target_comm in comms:
                print(f"  Sample edges to Community {target_comm}:")
                found = 0
                for v in G.neighbors(u):
                    if partition[v] == target_comm:
                        data = G.get_edge_data(u, v)
                        print(f"    --[{data['rel']}: {data['label']}]-- {G.nodes[v]['name']} (EID {v})")
                        found += 1
                        if found >= 3: break
    else:
        print("None found.")

    # Bridge Data
    bridge_data = []
    for u, v in bridges:
        edge = G.edges[u, v]
        name_u = G.nodes[u]['name']
        name_v = G.nodes[v]['name']
        c1, c2 = partition[u], partition[v]
        bridge_data.append({
            'u_name': name_u, 'u_comm': c1,
            'v_name': name_v, 'v_comm': c2,
            'rel': edge['rel'], 'label': edge['label']
        })
    
    df_bridges = pd.DataFrame(bridge_data)
    # Filter bridges that connect different communities
    inter_comm_bridges = df_bridges[df_bridges['u_comm'] != df_bridges['v_comm']]
    print("\nBridges connecting different communities:")
    if not inter_comm_bridges.empty:
        # Add parcel counts to bridge data
        bridge_data_with_counts = []
        for i, row in inter_comm_bridges.iterrows():
            c1_pc = df_comm.loc[df_comm['comm_id'] == row['u_comm'], 'parcel_count'].iloc[0]
            c2_pc = df_comm.loc[df_comm['comm_id'] == row['v_comm'], 'parcel_count'].iloc[0]
            bridge_data_with_counts.append({
                'u_name': row['u_name'], 'c1_pc': c1_pc,
                'v_name': row['v_name'], 'c2_pc': c2_pc,
                'rel': row['rel'], 'label': row['label']
            })
        df_bridges_with_counts = pd.DataFrame(bridge_data_with_counts)
        print(df_bridges_with_counts[['u_name', 'c1_pc', 'v_name', 'c2_pc', 'rel', 'label']].to_string(index=False))
    else:
        print("None found.")

if __name__ == "__main__":
    import sys
    cid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    investigate(cid)
