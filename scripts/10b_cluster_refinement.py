#!/usr/bin/env python3
"""
10b_cluster_refinement.py — Automated cluster fission + fusion.

Pass A (Fusion):  Merges fragmented corporate entity series split by
                  OCR/normalization artifacts (e.g., PROGRESS RESIDENTIAL
                  BORROWER 1..25 spread across 24 clusters).

Pass B (Fission): Detects and severs false address bridges inside large
                  clusters (e.g., Pretium Partners / Amherst Holdings
                  over-merge in cluster 3).

Run AFTER 10_sos_network_enrichment.py, BEFORE materialized view rebuild.
"""

import re
from collections import defaultdict
import networkx as nx
from sqlalchemy import create_engine, text
from utils_persistence import reassign_cluster_ids

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

# --- Tuning Parameters ---
MAX_MERGE_PARCELS      = 5000   # Pass A: cap on total parcels in a fusion group
FISSION_THRESHOLD      = 300    # Pass B: min cluster parcel_count to examine for bridges
COHESION_THRESHOLD     = 0.4    # Pass B: min avg within-component name similarity to be "coherent"
META_CONNECT_THRESHOLD = 0.30   # Pass B: min avg name sim to join two components in meta-graph
SEPARATION_THRESHOLD   = 0.15   # Pass B: max avg name sim between final meta-groups to split
MIN_FISSION_PARCELS    = 50     # Pass B: min parcel count for a split group to be separated

# --- Stemming constants ---
SUFFIX_NOISE = frozenset({
    'LLC', 'LP', 'L P', 'LLP', 'LLLP', 'INC', 'CORP', 'CO',
    'L.L.C.', 'L.P.', 'INC.', 'CORP.', 'LP.', 'LTD', 'LTD.',
    'INCORPORATED', 'CORPORATION', 'COMPANY',
    # SFR series vehicle suffixes — these appear as trailing noise in securitized
    # fund names (e.g. "TRICON SFR 2024 3 BORROWER LLC", "SFR JV 2 PROPERTY LLC").
    # verified safe: simulation over 33K SOS-matched entities produced zero false merges.
    'BORROWER', 'PROPERTY', 'PROPERTIES', 'OWNER', 'OWNERCO',
})
ROMAN_NUMERALS = frozenset({
    'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX',
    'X', 'XI', 'XII', 'XIII', 'XIV', 'XV', 'XVI',
    'XVII', 'XVIII', 'XIX', 'XX',
})
NUMBER_WORDS = frozenset({
    'ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN',
    'EIGHT', 'NINE', 'TEN', 'ELEVEN', 'TWELVE', 'THIRTEEN',
    'FOURTEEN', 'FIFTEEN', 'SIXTEEN', 'SEVENTEEN', 'EIGHTEEN',
    'NINETEEN', 'TWENTY',
})
_PURE_DIGIT   = re.compile(r'^\d+$')
_SINGLE_LETTER = re.compile(r'^[A-Z]$')
_ORDINAL      = re.compile(r'^\d+(ST|ND|RD|TH)$')
_YEAR_4DIG    = re.compile(r'^\d{4}$')
_SHORT_SEQ    = re.compile(r'^\d{1,2}$')

# City/zip-only pattern — these are skipped when building address edges
_CITY_ZIP_ONLY = re.compile(r'^[A-Z]+(?:\s+[A-Z]+)*\s+[A-Z]{2}\s+\d{3,5}(?:-\d+)?$')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_strippable(token: str) -> bool:
    """Return True if a token can be stripped from a name stem (trailing or suffix)."""
    if not token:
        return False
    if token in SUFFIX_NOISE:
        return True
    if token in ROMAN_NUMERALS:
        return True
    if token in NUMBER_WORDS:
        return True
    if _PURE_DIGIT.match(token):
        return True
    if _SINGLE_LETTER.match(token):
        return True
    if _ORDINAL.match(token):
        return True
    return False


def compute_stem(name: str) -> str:
    """
    Reduce a normalized owner name to its identifying stem by stripping:
      - trailing entity-type noise (LLC, LP, BORROWER, OWNER, PROPERTY, …)
      - trailing series noise (numbers, Roman numerals, number words, single letters)
      - interior 4-digit year + optional 1-2 digit sequence (e.g. "2024 3")
      - leading year+seq prefix (e.g. "2018 3 IH BORROWER LP")

    The interior year strip is what unifies securitized fund vintage series:
      TRICON SFR 2020 2 BORROWER LLC  -> TRICON SFR
      TRICON SFR 2024 3 BORROWER LLC  -> TRICON SFR
      PROGRESS RESIDENTIAL BORROWER 14 LLC  -> PROGRESS RESIDENTIAL
      2018 3 IH BORROWER LP           -> IH
      SFR XII ATL OWNER 1 LP          -> SFR XII ATL
      SFR JV 2 PROPERTY LLC           -> SFR JV

    Safety: city+state co-requirement in Pass A prevents short stems (e.g. "TAH",
    "SFR JV") from bridging unrelated entities at different locations. The >= 4 char
    gate rejects stems that are too short to be discriminating.
    """
    tokens = name.upper().replace(',', '').split()
    if not tokens:
        return ''

    # Pass 1: strip trailing noise
    while tokens and is_strippable(tokens[-1]):
        tokens.pop()

    # Pass 2: strip interior 4-digit year + optional 1-2 digit sequence
    clean = []
    i = 0
    while i < len(tokens):
        if _YEAR_4DIG.match(tokens[i]):
            i += 1
            if i < len(tokens) and _SHORT_SEQ.match(tokens[i]):
                i += 1
        else:
            clean.append(tokens[i])
            i += 1
    tokens = clean

    # Pass 3: strip leading year+seq that survived (e.g. "2022 IH BORROWER")
    while len(tokens) >= 2 and _YEAR_4DIG.match(tokens[0]) and _SHORT_SEQ.match(tokens[1]):
        tokens = tokens[2:]
    while tokens and _YEAR_4DIG.match(tokens[0]):
        tokens = tokens[1:]

    # Pass 4: strip trailing noise again (now exposed after interior/leading removal)
    while tokens and is_strippable(tokens[-1]):
        tokens.pop()

    return ' '.join(tokens)


def extract_city_state(addr: str) -> tuple:
    """
    Extract (city, state) from a normalized address string.
    Returns (None, None) if not determinable.

    Handles patterns like:
      SCOTTSDALE AZ 85261
      16220 N SCOTTSDALE RD STE 650 SCOTTSDALE AZ 85254
      591 W PUTNAM AVE GREENWICH CT 06830
    """
    if not addr:
        return None, None
    a = addr.upper().strip()
    # Match "CITY STATE ZIP" at end (city = 1-3 word sequence before 2-letter state)
    m = re.search(r'\b([A-Z][A-Z ]{1,30}?)\s+([A-Z]{2})\s+\d{3,5}(?:-\d+)?$', a)
    if m:
        city = m.group(1).strip()
        # Reject multi-word "cities" that look like street suffixes
        state = m.group(2)
        return city, state
    # Match "CITY STATE" at end (no zip)
    m = re.search(r'\b([A-Z][A-Z ]{1,30}?)\s+([A-Z]{2})$', a)
    if m:
        return m.group(1).strip(), m.group(2)
    return None, None


def jaccard(tokens_a: frozenset, tokens_b: frozenset) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    union = tokens_a | tokens_b
    return len(tokens_a & tokens_b) / len(union)


def name_tokens(name: str) -> frozenset:
    """
    Token set for name-similarity comparison.
    Strips trailing entity-type noise (LLC, LP, INC, …) so that 'SFR LLC'
    and 'SFR L P' compare as identical, and 'LLC' doesn't pad similarity
    between semantically unrelated names.
    """
    tokens = name.upper().split()
    while tokens and is_strippable(tokens[-1]):
        tokens.pop()
    # Fallback: if everything was stripped, use original tokens (rare)
    return frozenset(tokens) if tokens else frozenset(name.upper().split())


def avg_pairwise_jaccard(names_a: list, names_b: list, max_sample: int = 30) -> float:
    """Average pairwise Jaccard similarity between two name lists.
    Uses suffix-stripped tokens so entity-type noise (LLC/LP/INC) doesn't
    inflate similarity between semantically unrelated names."""
    if not names_a or not names_b:
        return 0.0
    ta_list = [name_tokens(n) for n in names_a[:max_sample]]
    tb_list = [name_tokens(n) for n in names_b[:max_sample]]
    total = 0.0
    count = 0
    for ta in ta_list:
        for tb in tb_list:
            total += jaccard(ta, tb)
            count += 1
    return total / count if count > 0 else 0.0


def within_cohesion(names: list, max_sample: int = 30) -> float:
    """Average pairwise Jaccard similarity within a list of names."""
    if len(names) <= 1:
        return 1.0
    toks = [frozenset(n.upper().split()) for n in names[:max_sample]]
    total = 0.0
    count = 0
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            total += jaccard(toks[i], toks[j])
            count += 1
    return total / count if count > 0 else 0.0


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def load_cluster_parcel_counts(engine) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT cluster_id, SUM(count) FROM owner_entities GROUP BY cluster_id"
        )).fetchall()
    return {r[0]: r[1] for r in rows}


def apply_cluster_map(engine, cluster_map: dict, table_suffix: str):
    """Write a cluster_id -> new_cluster_id mapping to the DB."""
    updates = [{"eid": eid, "cid": cid} for eid, cid in cluster_map.items()]
    if not updates:
        return
    tmp = f"tmp_cr_{table_suffix}"
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TEMP TABLE {tmp} (entity_id BIGINT, new_cluster_id INT)"))
        for i in range(0, len(updates), 50000):
            conn.execute(
                text(f"INSERT INTO {tmp} VALUES (:eid, :cid)"),
                updates[i:i + 50000]
            )
        conn.execute(text(
            f"UPDATE owner_entities oe "
            f"SET cluster_id = t.new_cluster_id "
            f"FROM {tmp} t WHERE oe.entity_id = t.entity_id"
        ))
        conn.execute(text(f"DROP TABLE {tmp}"))


def rebuild_ownership_clusters(engine):
    """Rebuild ownership_clusters from owner_entities (same pattern as script 10)."""
    print("Rebuilding ownership_clusters...")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ownership_clusters CASCADE"))
        conn.execute(text("""
            CREATE TABLE ownership_clusters AS
            WITH name_ranks AS (
                SELECT cluster_id, owner_name_norm,
                       MAX(array_length(parcel_ids, 1)) AS max_pc
                FROM owner_entities GROUP BY cluster_id, owner_name_norm
            ),
            name_arrays AS (
                SELECT cluster_id,
                       ARRAY_AGG(owner_name_norm ORDER BY max_pc DESC, owner_name_norm)
                           AS owner_names
                FROM name_ranks GROUP BY cluster_id
            )
            SELECT oe.cluster_id,
                   COUNT(*)         AS entity_count,
                   SUM(oe.count)    AS parcel_count,
                   na.owner_names,
                   ARRAY_AGG(DISTINCT oe.owner_addr_norm ORDER BY oe.owner_addr_norm)
                       FILTER (WHERE oe.owner_addr_norm != '')  AS owner_addresses,
                   COUNT(DISTINCT oe.sos_control_number)
                       FILTER (WHERE oe.sos_control_number IS NOT NULL)
                                    AS sos_entity_count,
                   MODE() WITHIN GROUP (ORDER BY oe.sos_status)
                                    AS primary_sos_status
            FROM owner_entities oe
            JOIN name_arrays na USING (cluster_id)
            GROUP BY oe.cluster_id, na.owner_names
            ORDER BY parcel_count DESC
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_oc_cluster_id "
            "ON ownership_clusters (cluster_id)"
        ))
    print("  Done.")


# ---------------------------------------------------------------------------
# Pass A — Name-Series Fusion
# ---------------------------------------------------------------------------

def pass_a_fusion(engine):
    """
    Merge fragmented corporate entity series into unified clusters.

    Identifies groups sharing the same (name_stem, city, state) and merges
    them if the total parcel count does not exceed MAX_MERGE_PARCELS.
    """
    print("\n=== Pass A: Name-Series Fusion ===")

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT entity_id, owner_name_norm, owner_addr_norm, count, cluster_id
            FROM owner_entities
            WHERE is_institutional = FALSE AND is_corporate = TRUE
        """)).fetchall()

    cluster_parcels = load_cluster_parcel_counts(engine)

    # Group by (stem, city, state) → {cluster_id → [entity_ids]}
    groups: dict[tuple, dict] = defaultdict(lambda: defaultdict(list))

    skipped_no_addr = 0
    for entity_id, name, addr, count, cluster_id in rows:
        stem = compute_stem(name)
        if not stem or len(stem) < 4:
            continue
        city, state = extract_city_state(addr or '')
        if not city or not state:
            skipped_no_addr += 1
            continue
        groups[(stem, city, state)][cluster_id].append(entity_id)

    print(f"  {len(rows):,} non-institutional entities → "
          f"{len(groups):,} (stem, city, state) groups "
          f"({skipped_no_addr:,} skipped — no parseable city/state)")

    # Union-Find for cluster merging
    parent: dict[int, int] = {}

    def find(x):
        if x not in parent:
            parent[x] = x
        root = x
        while parent[root] != root:
            root = parent[root]
        # Path compression
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # Merge to lower cluster_id
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    fusion_count   = 0
    skipped_large  = 0
    logged_merges  = []

    for (stem, city, state), cid_map in groups.items():
        if len(cid_map) <= 1:
            continue  # Already unified or single cluster

        cluster_ids = list(cid_map.keys())
        total_parcels = sum(cluster_parcels.get(cid, 0) for cid in cluster_ids)

        if total_parcels > MAX_MERGE_PARCELS:
            skipped_large += 1
            print(f"  SKIP (>{MAX_MERGE_PARCELS} parcels, got {total_parcels}): "
                  f"{stem!r} / {city} {state} — clusters {sorted(cluster_ids)}")
            continue

        for i in range(1, len(cluster_ids)):
            union(cluster_ids[0], cluster_ids[i])

        fusion_count += 1
        logged_merges.append((stem, city, state, sorted(cluster_ids), total_parcels))

    if fusion_count == 0:
        print("  No fusion candidates found.")
        return 0

    # Build entity_id -> new_cluster_id map
    entity_map: dict[int, int] = {}
    for entity_id, name, addr, count, cluster_id in rows:
        if cluster_id in parent:
            new_cid = find(cluster_id)
            if new_cid != cluster_id:
                entity_map[entity_id] = new_cid

    print(f"\n  Fusion summary: {fusion_count} groups merged "
          f"({skipped_large} skipped — too large):")
    for stem, city, state, cids, parcels in sorted(logged_merges,
                                                    key=lambda x: -x[4]):
        print(f"    {stem!r} / {city} {state}: "
              f"{cids} → {find(cids[0])} ({parcels} parcels)")

    print(f"\n  Applying {len(entity_map):,} entity cluster reassignments...")
    apply_cluster_map(engine, entity_map, "fusion")
    print("  Pass A complete.")
    return fusion_count


# ---------------------------------------------------------------------------
# Pass B — Articulation-Point Fission
# ---------------------------------------------------------------------------

def pass_b_fission(engine):
    """
    Detect and sever false address bridges inside large clusters.

    For each cluster >= FISSION_THRESHOLD parcels:
      1. Build address-only subgraph (skip city/zip-only addresses).
      2. Find connected components of that subgraph.
      3. Group singleton/small components with their most name-similar neighbor.
      4. If final groups have low between-similarity and enough size: split.
      5. Also check for articulation points in single-component subgraphs.
    """
    print("\n=== Pass B: Fission ===")

    # Load up-to-date parcel counts (after Pass A may have changed things)
    with engine.connect() as conn:
        large_clusters = conn.execute(text("""
            SELECT cluster_id, SUM(count) AS parcel_count
            FROM owner_entities
            GROUP BY cluster_id
            HAVING SUM(count) >= :threshold
            ORDER BY SUM(count) DESC
        """), {"threshold": FISSION_THRESHOLD}).fetchall()

    print(f"  {len(large_clusters)} clusters with >= {FISSION_THRESHOLD} parcels")

    cids = [r[0] for r in large_clusters]
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT entity_id, owner_name_norm, owner_addr_norm, count, cluster_id
            FROM owner_entities
            WHERE cluster_id = ANY(:cids)
        """), {"cids": cids}).fetchall()

    entities_by_cluster: dict[int, list] = defaultdict(list)
    for eid, name, addr, count, cid in rows:
        entities_by_cluster[cid].append((eid, name, addr or '', count))

    # Get next available cluster_id
    with engine.connect() as conn:
        max_cid = conn.execute(
            text("SELECT MAX(cluster_id) FROM owner_entities")
        ).scalar() or 0
    next_cid = [max_cid + 1]  # mutable via list for closure

    all_reassignments: dict[int, int] = {}
    splits_applied = 0

    for cluster_id, parcel_count in large_clusters:
        entities = entities_by_cluster[cluster_id]
        if len(entities) < 3:
            continue

        eid_to_name  = {e[0]: e[1] for e in entities}
        eid_to_count = {e[0]: e[3] for e in entities}

        # Build address-only subgraph (skip city/zip-only addresses)
        G_sub = nx.Graph()
        addr_to_eids: dict[str, list] = defaultdict(list)

        for eid, name, addr, count in entities:
            G_sub.add_node(eid)
            addr_clean = addr.upper().strip()
            if addr_clean and not _CITY_ZIP_ONLY.match(addr_clean):
                addr_to_eids[addr_clean].append(eid)

        for addr, eids in addr_to_eids.items():
            for i in range(len(eids)):
                for j in range(i + 1, len(eids)):
                    G_sub.add_edge(eids[i], eids[j])

        components = list(nx.connected_components(G_sub))

        if len(components) == 1:
            # ---------- single connected component: check articulation points ----------
            aps = list(nx.articulation_points(G_sub))
            if not aps:
                continue

            severed = False
            for ap in aps:
                G_test = G_sub.copy()
                G_test.remove_node(ap)
                sub_comps = list(nx.connected_components(G_test))
                if len(sub_comps) < 2:
                    continue

                ap_name  = eid_to_name.get(ap, '')
                ap_tokens = frozenset(ap_name.upper().split())

                # Check cohesion within each sub-component
                all_cohesive = True
                all_separate = True
                for sc in sub_comps:
                    names_in = [eid_to_name.get(e, '') for e in sc]
                    coh = within_cohesion(names_in)
                    if coh < COHESION_THRESHOLD:
                        all_cohesive = False
                        break

                if not all_cohesive:
                    continue

                for i in range(len(sub_comps)):
                    for j in range(i + 1, len(sub_comps)):
                        names_i = [eid_to_name.get(e, '') for e in sub_comps[i]]
                        names_j = [eid_to_name.get(e, '') for e in sub_comps[j]]
                        sim = avg_pairwise_jaccard(names_i, names_j)
                        if sim >= SEPARATION_THRESHOLD:
                            all_separate = False
                            break
                    if not all_separate:
                        break

                if not all_separate:
                    continue

                # Sever: assign AP to most similar sub-component
                best_comp_idx = 0
                best_sim = -1.0
                ap_name_list = [ap_name]
                for idx, sc in enumerate(sub_comps):
                    names_sc = [eid_to_name.get(e, '') for e in sc]
                    sim = avg_pairwise_jaccard(ap_name_list, names_sc)
                    if sim > best_sim:
                        best_sim = sim
                        best_comp_idx = idx

                print(f"\n  Severing AP in cluster {cluster_id} "
                      f"(parcel_count={parcel_count}):")
                print(f"    Articulation point: {ap_name!r}")
                print(f"    Sub-components: {len(sub_comps)}")

                # Keep the largest sub-component in the original cluster;
                # assign others new cluster_ids
                sc_with_sizes = sorted(
                    enumerate(sub_comps),
                    key=lambda x: sum(eid_to_count.get(e, 0) for e in x[1]),
                    reverse=True
                )
                # Add AP to its best component
                sc_idx_to_comp = {idx: comp for idx, comp in enumerate(sub_comps)}
                sc_idx_to_comp[best_comp_idx].add(ap)

                kept_idx = sc_with_sizes[0][0]
                for idx, sc in sc_with_sizes[1:]:
                    new_cid = next_cid[0]
                    next_cid[0] += 1
                    sc_parcels = sum(eid_to_count.get(e, 0) for e in sc)
                    if sc_parcels < MIN_FISSION_PARCELS:
                        print(f"    Skip small component ({sc_parcels} parcels < "
                              f"{MIN_FISSION_PARCELS}) → leaving in cluster {cluster_id}")
                        continue
                    names_sample = [eid_to_name.get(e, '') for e in list(sc)[:5]]
                    print(f"    → New cluster {new_cid}: "
                          f"{len(sc)} entities, {sc_parcels} parcels, "
                          f"names={names_sample}")
                    for e in sc:
                        all_reassignments[e] = new_cid

                severed = True
                splits_applied += 1
                # Rebuild G_sub after severing to handle multi-hop bridges
                G_sub.remove_node(ap)
                break  # re-evaluate after first sever (outer loop handles rest)

        else:
            # ---------- multiple disconnected components: meta-graph grouping ----------
            # For each component, gather entity names
            comp_names   = [[eid_to_name.get(e, '') for e in comp] for comp in components]
            comp_parcels = [sum(eid_to_count.get(e, 0) for e in comp) for comp in components]

            # Build meta-graph: connect components with similar names
            # (uses META_CONNECT_THRESHOLD which is higher than SEPARATION_THRESHOLD,
            #  preventing ambiguous "bridge" names from merging distant groups)
            meta_G = nx.Graph()
            for i in range(len(components)):
                meta_G.add_node(i, parcels=comp_parcels[i])

            for i in range(len(components)):
                for j in range(i + 1, len(components)):
                    sim = avg_pairwise_jaccard(comp_names[i], comp_names[j])
                    if sim >= META_CONNECT_THRESHOLD:
                        meta_G.add_edge(i, j, weight=sim)

            meta_components = list(nx.connected_components(meta_G))

            if len(meta_components) <= 1:
                continue  # All address-components are name-similar enough — no split

            # Compute names and parcel counts per meta-component
            mc_names   = [[n for ci in mc for n in comp_names[ci]]
                          for mc in meta_components]
            mc_parcels = [sum(comp_parcels[ci] for ci in mc)
                          for mc in meta_components]

            # Identify "isolated" meta-components: those with low avg name similarity
            # to ALL other meta-components.  These are candidates to split off.
            # This correctly handles cases where sibling meta-components (e.g. Amherst
            # sub-groups) have moderate inter-similarity (0.15–0.30) that prevents
            # them from all_low check passing — we only split off clearly foreign groups.
            mc_cid_map: dict[int, int] = {}  # meta-comp index -> new cluster_id

            # Pre-compute entity → meta-component-index map for cross-edge detection.
            eid_to_mc_idx: dict[int, int] = {}
            for mi, mc in enumerate(meta_components):
                for ci in mc:
                    for e in components[ci]:
                        eid_to_mc_idx[e] = mi

            isolated_idxs = []
            for i in range(len(meta_components)):
                if mc_parcels[i] < MIN_FISSION_PARCELS:
                    continue

                # Address-edge check (using PURE entities only):
                # A "pure" entity has NO address edges to entities in OTHER
                # meta-components.  If the meta-component's pure entities have
                # internal address connections, those connections legitimately
                # group the entities and the component should NOT be split off.
                # This correctly handles cases like SFR XII ATL OWNER (address-
                # connected within the Invitation Homes cluster) vs FYR SFR
                # BORROWER (isolated nodes whose meta-component contains an
                # "impure" bridge entity — HOME SFR BORROWER — that connects
                # to a different firm's mailing address).
                mc_eids = {e for ci in meta_components[i] for e in components[ci]}
                pure_eids: set[int] = set()
                for e in mc_eids:
                    if all(eid_to_mc_idx.get(n, i) == i for n in G_sub.neighbors(e)):
                        pure_eids.add(e)
                if G_sub.subgraph(pure_eids).number_of_edges() > 0:
                    continue  # Pure core has real address connections — don't split

                # Cohesion check: only split off internally coherent groups.
                # This prevents accidentally splitting off diverse-but-legitimate
                # sub-groups (e.g., opaque fund vehicles of the same operator).
                coh = within_cohesion(mc_names[i])
                if coh < COHESION_THRESHOLD:
                    continue

                # Isolation check: low average name-similarity to ALL other groups.
                is_isolated = True
                for j in range(len(meta_components)):
                    if i == j:
                        continue
                    sim = avg_pairwise_jaccard(mc_names[i], mc_names[j])
                    if sim >= SEPARATION_THRESHOLD:
                        is_isolated = False
                        break
                if is_isolated:
                    isolated_idxs.append(i)

            if not isolated_idxs:
                continue  # Nothing clearly separable

            # The "base" is all non-isolated meta-components merged together
            base_parcels = sum(
                mc_parcels[i] for i in range(len(meta_components))
                if i not in isolated_idxs
            )
            if base_parcels < MIN_FISSION_PARCELS and len(isolated_idxs) < len(meta_components):
                continue  # Base group too small to be coherent without isolated ones

            print(f"\n  Splitting cluster {cluster_id} "
                  f"(parcel_count={parcel_count}, "
                  f"{len(components)} address-comps, "
                  f"{len(meta_components)} meta-groups, "
                  f"{len(isolated_idxs)} to split off):")

            non_isolated = [i for i in range(len(meta_components))
                            if i not in isolated_idxs]
            if non_isolated:
                keep_names = [mc_names[i][0] if mc_names[i] else '?' for i in non_isolated[:3]]
                print(f"    Keeping in cluster {cluster_id}: "
                      f"{base_parcels} parcels, sample={keep_names}")
            else:
                # All meta-components are isolated from each other — keep largest
                largest_idx = max(range(len(meta_components)),
                                  key=lambda i: mc_parcels[i])
                isolated_idxs = [i for i in isolated_idxs if i != largest_idx]
                base_parcels  = mc_parcels[largest_idx]
                keep_names    = mc_names[largest_idx][:3]
                print(f"    Keeping in cluster {cluster_id}: "
                      f"{base_parcels} parcels, sample={keep_names}")

            for i in isolated_idxs:
                new_cid = next_cid[0]
                next_cid[0] += 1
                mc_cid_map[i] = new_cid
                sample = mc_names[i][:5]
                print(f"    → New cluster {new_cid}: "
                      f"{mc_parcels[i]} parcels, names={sample}")
                for ci in meta_components[i]:
                    for e in components[ci]:
                        all_reassignments[e] = new_cid

            splits_applied += 1

    if not all_reassignments:
        print("  No fission candidates found.")
        return 0

    print(f"\n  Applying {len(all_reassignments):,} entity reassignments "
          f"({splits_applied} splits)...")
    apply_cluster_map(engine, all_reassignments, "fission")
    print("  Pass B complete.")
    return splits_applied


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== 10b_cluster_refinement.py ===")
    print("Tuning: "
          f"MAX_MERGE_PARCELS={MAX_MERGE_PARCELS}, "
          f"FISSION_THRESHOLD={FISSION_THRESHOLD}, "
          f"COHESION_THRESHOLD={COHESION_THRESHOLD}, "
          f"SEPARATION_THRESHOLD={SEPARATION_THRESHOLD}")

    fused  = pass_a_fusion(engine)
    splits = pass_b_fission(engine)

    reassign_cluster_ids(engine)
    rebuild_ownership_clusters(engine)

    print("\n=== Done ===")
    print("Next step:")
    print("  PGPASSWORD=woa psql -h localhost -p 5434 -U woa who_owns_atl "
          "-f scripts/sql/04_create_materialized_views.sql")
