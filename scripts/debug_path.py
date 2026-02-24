import networkx as nx
from sqlalchemy import create_engine, text
import re
import sys

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

def normalize_street(addr: str) -> str:
    if not addr: return ""
    s = re.sub(r'[.,?]', '', addr).strip()
    return re.sub(r'\s+(STE|SUITE|UNIT|BLDG|OFFICE|#|APT)\s+.*$', '', s, flags=re.IGNORECASE).strip()

def find_path(cid):
    print(f"Loading entities for Cluster {cid}...")
    with engine.connect() as conn:
        entities = conn.execute(text("""
            SELECT entity_id, owner_name_norm, owner_addr_norm,
                   sos_control_number, sos_registered_agent, sos_registered_agent_address,
                   sos_match_type
            FROM owner_entities WHERE cluster_id = :cid
        """), {"cid": cid}).fetchall()
    
    G = nx.Graph()
    name_idx = {}
    addr_idx = {}
    eid_to_name = {}

    for row in entities:
        eid, name, addr = row[0], row[1], row[2]
        G.add_node(eid, name=name)
        eid_to_name[eid] = name
        name_idx.setdefault(name, []).append(eid)
        if addr: addr_idx.setdefault(addr, []).append(eid)

    # Add edges
    for name, eids in name_idx.items():
        for i in range(len(eids)):
            for j in range(i+1, len(eids)):
                G.add_edge(eids[i], eids[j], rel="same_name", label=name)
    
    for addr, eids in addr_idx.items():
        for i in range(len(eids)):
            for j in range(i+1, len(eids)):
                G.add_edge(eids[i], eids[j], rel="same_addr", label=addr)

    # SOS Edges
    cns = [row[3] for row in entities if row[3]]
    if cns:
        with engine.connect() as conn:
            off_rows = conn.execute(text("SELECT control_number, first_name, last_name FROM sos.officers WHERE control_number = ANY(:cns)"), {"cns": cns}).fetchall()
            sos_addr_rows = conn.execute(text("SELECT control_number, street_address1, city, state FROM sos.addresses WHERE control_number = ANY(:cns)"), {"cns": cns}).fetchall()
        
        cn_to_eids = {}
        for row in entities:
            if row[3]: cn_to_eids.setdefault(row[3], []).append(row[0])
            
        off_idx = {}
        for cn, fn, ln in off_rows:
            if fn and ln: off_idx.setdefault(f"{fn} {ln}".upper(), []).extend(cn_to_eids.get(cn, []))
        for off, eids in off_idx.items():
            eids = list(set(eids))
            for i in range(len(eids)):
                for j in range(i+1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="shared_officer", label=off)

        sos_addr_idx = {}
        for cn, street, city, state in sos_addr_rows:
            if street: sos_addr_idx.setdefault(f"{street} {city} {state}".upper(), []).extend(cn_to_eids.get(cn, []))
        for addr, eids in sos_addr_idx.items():
            eids = list(set(eids))
            for i in range(len(eids)):
                for j in range(i+1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="shared_sos_addr", label=addr)

    # Find path
    baf_nodes = [n for n, d in G.nodes(data=True) if "BAF ASSETS" in d['name']]
    fyr_nodes = [n for n, d in G.nodes(data=True) if "FYR SFR" in d['name']]
    
    if not baf_nodes or not fyr_nodes:
        print("Could not find BAF or FYR nodes.")
        return

    try:
        path = nx.shortest_path(G, baf_nodes[0], fyr_nodes[0])
        print("\nShortest path from BAF to FYR:")
        for i in range(len(path)-1):
            u, v = path[i], path[i+1]
            edge = G.edges[u, v]
            print(f"  {eid_to_name[u]} --({edge['rel']}: {edge['label']})--> {eid_to_name[v]}")
    except nx.NetworkXNoPath:
        print("No path found.")

if __name__ == "__main__":
    cid = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    find_path(cid)
