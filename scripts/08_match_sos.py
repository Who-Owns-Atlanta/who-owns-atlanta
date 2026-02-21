"""Match parcel corporate owner names against GA SOS entity names using multi-processing.

Refined Strategy:
  1. Split multi-entity names on '&', 'AND', 'ET AL'.
  2. Phase 1 (Exact): Direct SQL JOIN on normalized name for all parts.
  3. Phase 2 (Parallel Fuzzy): Hybrid Indexed Fuzzy Match for remaining unmatched names.
     - Spawns multiple worker processes (one per core/specified).
     - Each worker uses PostgreSQL GIN trigram index and rapidfuzz.
"""

import argparse
import re
import multiprocessing
import psycopg2
from rapidfuzz import fuzz

_PUNCT = re.compile(r'[-,;/()]+')

def _cmp_norm(s):
    if not s: return ""
    return re.sub(r'\s+', ' ', _PUNCT.sub(' ', s)).strip()

DB = dict(host="localhost", port=5434, dbname="who_owns_atl", user="woa", password="woa")

# SQL setup statements
SETUP_STMTS = [
    r"""
    CREATE OR REPLACE FUNCTION normalize_biz_name(txt TEXT) RETURNS TEXT
    LANGUAGE sql IMMUTABLE STRICT AS $$
        SELECT regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(
                                upper(trim(txt)),
                                '[^A-Z0-9\s]', '', 'g'
                            ),
                            '\mL\s+L\s+P\M', 'LLP', 'g'
                        ),
                        '\mL\s+L\s+C\M', 'LLC', 'g'
                    ),
                    '\mL\s+P\M', 'LP', 'g'
                ),
                '\s+', ' ', 'g'
            ),
            '^\s+|\s+$', '', 'g'
        )
    $$
    """,
    "ALTER TABLE sos.entities ADD COLUMN IF NOT EXISTS biz_name_norm TEXT",
    "UPDATE sos.entities SET biz_name_norm = normalize_biz_name(business_name) WHERE biz_name_norm IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_sos_entities_biz_name_norm ON sos.entities (biz_name_norm)",
    "CREATE INDEX IF NOT EXISTS idx_sos_entities_biz_name_norm_trgm ON sos.entities USING gin (biz_name_norm gin_trgm_ops)",
    "CREATE TABLE IF NOT EXISTS sos_matches (owner_name_norm TEXT, sos_control_number TEXT, sos_business_id TEXT, sos_business_name TEXT, sos_biz_name_norm TEXT, match_type TEXT, similarity FLOAT, entity_status TEXT, business_type TEXT, foreign_state TEXT)",
    "CREATE TABLE IF NOT EXISTS _parcel_owners_split (full_owner_norm TEXT, part_norm TEXT)",
]

def split_name(name):
    parts = re.split(r'\s+&\s+|\s+AND\s+|\s+ET\s+AL\s*$', name, flags=re.IGNORECASE)
    return [p.strip() for p in parts if len(p.strip()) > 2]

def fuzzy_worker(worker_id, chunk):
    """Worker process for Phase 2 fuzzy matching."""
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("SET pg_trgm.similarity_threshold = 0.4")
    
    inserts = []
    matched_count = 0
    total = len(chunk)
    
    print(f"Worker {worker_id} started: processing {total:,} names")

    for i, (full_name, part) in enumerate(chunk):
        if i % 100 == 0 and i > 0:
            print(f"Worker {worker_id} progress: {i:,}/{total:,} — Matched: {matched_count:,}")

        # Strip common suffixes for the INDEX lookup only to avoid generic matches
        # but keep them for the final scoring
        search_part = re.sub(r'\b(LLC|INC|CORP|LTD|LP|LLP|PROPERTIES|HOLDINGS|MANAGEMENT|GROUP|INVESTMENTS)\b', '', part).strip()
        if len(search_part) < 3:
            search_part = part

        # Fuzzy lookup via Index
        cur.execute("""
            SELECT 
                biz_name_norm, control_number, business_id,
                business_name, entity_status, business_type_desc, foreign_state,
                similarity(biz_name_norm, %s) as sql_sim,
                registered_agent_id
            FROM sos.entities
            WHERE biz_name_norm %% %s
            ORDER BY sql_sim DESC
            LIMIT 50
        """, (part, search_part))
        candidates = cur.fetchall()
        
        if not candidates:
            continue

        part_cmp = _cmp_norm(part)
        best_score = -1
        best_row = None
        
        for cand in candidates:
            # Score similarity
            sim_score = fuzz.token_sort_ratio(part_cmp, _cmp_norm(cand[0]))
            
            # Penalize non-active and placeholder types
            status_score = 1.0
            if cand[4] and not cand[4].startswith('Active'):
                status_score = 0.8
            if cand[5] and 'Name Reservation' in cand[5]:
                status_score = 0.5
            if cand[8] == '0' or not cand[8]:
                status_score *= 0.9
                
            final_score = sim_score * status_score
            
            if final_score > best_score:
                best_score = final_score
                best_row = cand
        
        if best_row and (best_score >= 65 or (best_score >= 60 and 'LLC' in part)):
            match_type = 'trgm_high' if best_score >= 80 else 'trgm_low'
            inserts.append((
                full_name, best_row[1], best_row[2], best_row[3], best_row[0],
                match_type, best_score / 100.0,
                best_row[4], best_row[5], best_row[6]
            ))
            matched_count += 1

        if len(inserts) >= 100:
            cur.executemany("INSERT INTO sos_matches VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", inserts)
            conn.commit()
            inserts = []

    if inserts:
        cur.executemany("INSERT INTO sos_matches VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", inserts)
        conn.commit()

    conn.close()
    print(f"Worker {worker_id} finished: matched {matched_count:,}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cores", type=int, default=12, help="Number of worker processes")
    parser.add_argument("--reset", action="store_true", help="Clear sos_matches before starting")
    parser.add_argument("--sample", type=int, help="Run on N sample names")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB)
    cur = conn.cursor()

    print("Setting up tables and indexes...")
    for stmt in SETUP_STMTS:
        cur.execute(stmt)
    
    if args.reset:
        print("Resetting sos_matches table...")
        cur.execute("TRUNCATE sos_matches")
        cur.execute("TRUNCATE _parcel_owners_split")
        
        # Phase 0: Pre-calculate splits
        print("Pre-calculating name splits...")
        cur.execute("""
           SELECT DISTINCT normalize_biz_name(owner) FROM fulton_parcels 
           WHERE is_corporate = TRUE AND owner IS NOT NULL AND trim(owner) <> ''
           UNION
           SELECT DISTINCT normalize_biz_name(ownernme1) FROM dekalb_parcels 
           WHERE is_corporate = TRUE AND ownernme1 IS NOT NULL AND trim(ownernme1) <> ''
        """)
        all_full_names = [r[0] for r in cur.fetchall()]
        
        split_data = []
        for full in all_full_names:
            for part in split_name(full):
                split_data.append((full, part))
        
        cur.executemany("INSERT INTO _parcel_owners_split VALUES (%s, %s)", split_data)
        conn.commit()

        # Phase 1: Exact Match (SQL)
        print("Phase 1: Running exact matches...")
        cur.execute("""
            INSERT INTO sos_matches
            SELECT DISTINCT ON (s.full_owner_norm)
                s.full_owner_norm, e.control_number, e.business_id, e.business_name, 
                e.biz_name_norm, 'exact', 1.0, e.entity_status, e.business_type_desc, e.foreign_state
            FROM _parcel_owners_split s
            JOIN sos.entities e ON e.biz_name_norm = s.part_norm
            ORDER BY s.full_owner_norm,
                     (CASE WHEN e.entity_status LIKE 'Active%' THEN 0 ELSE 1 END),
                     (CASE WHEN e.registered_agent_id <> '0' AND e.registered_agent_id <> '' THEN 0 ELSE 1 END),
                     (CASE WHEN e.business_type_desc NOT LIKE '%Name Reservation%' THEN 0 ELSE 1 END),
                     e.control_number DESC
        """)
        print(f"  Matched {cur.rowcount:,} parts exactly.")
        conn.commit()

    # Phase 2: Fuzzy Match (Parallel)
    print("Phase 2: Finding remaining unmatched parts...")
    query = """
        SELECT DISTINCT full_owner_norm, part_norm FROM _parcel_owners_split
        WHERE full_owner_norm NOT IN (SELECT owner_name_norm FROM sos_matches)
    """
    if args.sample:
        query += f" LIMIT {args.sample}"
        
    cur.execute(query)
    remaining = cur.fetchall()
    conn.close()
    
    total_remaining = len(remaining)
    print(f"  {total_remaining:,} parts still need matching. Starting {args.cores} workers.")

    # Split work into chunks for cores
    chunk_size = (total_remaining // args.cores) + 1
    chunks = [remaining[i:i + chunk_size] for i in range(0, total_remaining, chunk_size)]

    processes = []
    for idx, chunk in enumerate(chunks):
        p = multiprocessing.Process(target=fuzzy_worker, args=(idx, chunk))
        p.start()
        processes.append(p)

    for p in processes:
        p.join()

    print("\nAll matching complete.")

if __name__ == "__main__":
    main()
