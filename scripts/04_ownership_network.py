"""Build ownership network: group parcels by owner name and mailing address.

Strategy:
  1. Normalize owner names (upper, strip whitespace)
  2. Group by (owner_name_norm, owner_addr_norm) to create "owner entities"
  3. Use networkx to connect entities that share an address or a name
  4. Each connected component = an ownership cluster

Output: owner_clusters table with cluster_id assigned to each parcel.
"""

import networkx as nx
from sqlalchemy import create_engine, text

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)


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

    # Index: name -> [entity_ids], addr -> [entity_ids]
    name_idx: dict[str, list[int]] = {}
    addr_idx: dict[str, list[int]] = {}

    for eid, name, addr in entities:
        G.add_node(eid)
        name_idx.setdefault(name, []).append(eid)
        if addr:
            addr_idx.setdefault(addr, []).append(eid)

    # Connect entities that share the same name
    print("Connecting by shared owner name...")
    name_edges = 0
    for name, eids in name_idx.items():
        if len(eids) > 1:
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="same_name")
                    name_edges += 1
    print(f"  {name_edges:,} name edges")

    # Connect entities that share the same mailing address
    # Skip city/zip-only addresses (PO Box artifacts from libpostal stripping box numbers)
    import re
    city_zip_only = re.compile(r'^[A-Z]+(\s+[A-Z]+)*\s+[A-Z]{2}\s+\d{5}(-\d+)?$')

    print("Connecting by shared owner address...")
    addr_edges = 0
    skipped_city_zip = 0
    for addr, eids in addr_idx.items():
        if city_zip_only.match(addr):
            skipped_city_zip += 1
            continue
        if len(eids) > 1 and len(eids) <= 100:
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    G.add_edge(eids[i], eids[j], rel="same_addr")
                    addr_edges += 1
    print(f"  {addr_edges:,} address edges (skipped {skipped_city_zip:,} city/zip-only addresses)")

    print(f"  Graph: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")
    return G


def assign_clusters(engine, G):
    """Find connected components and assign cluster IDs."""
    print("Finding connected components...")
    components = list(nx.connected_components(G))
    print(f"  {len(components):,} clusters")

    # Sort by size descending
    components.sort(key=len, reverse=True)

    # Build entity_id -> cluster_id mapping
    cluster_map = {}
    for cluster_id, component in enumerate(components, 1):
        for eid in component:
            cluster_map[eid] = cluster_id

    # Write to DB via temp table + bulk join (much faster than row-by-row)
    print("Writing cluster assignments...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE owner_entities ADD COLUMN IF NOT EXISTS cluster_id INT;"))
        conn.execute(text("CREATE TEMP TABLE tmp_clusters (entity_id BIGINT, cluster_id INT);"))

        # Insert mappings in chunks
        updates = [{"eid": eid, "cid": cid} for eid, cid in cluster_map.items()]
        CHUNK = 50000
        for i in range(0, len(updates), CHUNK):
            conn.execute(
                text("INSERT INTO tmp_clusters (entity_id, cluster_id) VALUES (:eid, :cid)"),
                updates[i:i+CHUNK]
            )
            print(f"  Inserted {min(i+CHUNK, len(updates)):,} / {len(updates):,} mappings")

        # Single bulk update via join
        print("  Applying bulk UPDATE...")
        conn.execute(text("""
            UPDATE owner_entities oe
            SET cluster_id = tc.cluster_id
            FROM tmp_clusters tc
            WHERE oe.entity_id = tc.entity_id;
        """))
        conn.execute(text("DROP TABLE tmp_clusters;"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oe_cluster ON owner_entities (cluster_id);"))

    # Create cluster summary
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ownership_clusters CASCADE;"))
        conn.execute(text("""
            CREATE TABLE ownership_clusters AS
            WITH name_ranks AS (
                -- Deduplicate names per cluster, ordered by entity parcel count DESC
                -- so the largest entity name sorts first (primary display name).
                SELECT cluster_id, owner_name_norm,
                       MAX(array_length(parcel_ids, 1)) AS max_pc
                FROM owner_entities
                GROUP BY cluster_id, owner_name_norm
            ),
            name_arrays AS (
                SELECT cluster_id,
                       ARRAY_AGG(owner_name_norm ORDER BY max_pc DESC, owner_name_norm)
                           AS owner_names
                FROM name_ranks
                GROUP BY cluster_id
            ),
            addr_arrays AS (
                SELECT cluster_id,
                       ARRAY_AGG(DISTINCT owner_addr_norm ORDER BY owner_addr_norm)
                           FILTER (WHERE owner_addr_norm != '') AS owner_addresses
                FROM owner_entities
                GROUP BY cluster_id
            )
            SELECT oe.cluster_id,
                   COUNT(*)        AS entity_count,
                   SUM(oe.count)   AS parcel_count,
                   na.owner_names,
                   aa.owner_addresses
            FROM owner_entities oe
            JOIN name_arrays na USING (cluster_id)
            JOIN addr_arrays aa USING (cluster_id)
            GROUP BY oe.cluster_id, na.owner_names, aa.owner_addresses
            ORDER BY parcel_count DESC;
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oc_cluster ON ownership_clusters (cluster_id);"))

    # Stats
    with engine.connect() as conn:
        multi = conn.execute(text(
            "SELECT COUNT(*) FROM ownership_clusters WHERE entity_count > 1"
        )).scalar()
        top = conn.execute(text("""
            SELECT cluster_id, parcel_count, entity_count, owner_names[1:3]
            FROM ownership_clusters
            ORDER BY parcel_count DESC LIMIT 20
        """)).fetchall()

    print(f"  {multi:,} clusters with multiple entities (linked owners)")
    print(f"\nTop 20 ownership clusters:")
    for row in top:
        names = row.owner_names
        print(f"  Cluster {row.cluster_id}: {row.parcel_count:,} parcels, "
              f"{row.entity_count} entities — {names}")

    return len(components)


if __name__ == "__main__":
    build_owner_entities(engine)
    G = build_network(engine)
    assign_clusters(engine, G)
    print("\nDone.")
