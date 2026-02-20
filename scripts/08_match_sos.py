"""Match parcel corporate owner names against GA SOS entity names.

Strategy (in priority order):
  1. Exact normalized match  — 'exact'
  2. Trigram similarity ≥ 0.80  — 'trgm_high'
  3. Trigram similarity 0.65–0.79  — 'trgm_low'

Output: public.sos_matches
  One row per (parcel_owner_norm, sos_control_number) pair.
  For trgm matches, only the single best SOS match per owner name is kept.

Normalization applied to both sides:
  - Uppercase + strip
  - Remove periods
  - Collapse whitespace
  - "L L C" / "L.L.C." → "LLC"
  - "L P" / "L.P." → "LP"
  - "L L P" → "LLP"
"""

import collections
import re
import psycopg2
from rapidfuzz import fuzz

# Strip punctuation not already removed by SQL normalization (hyphens, commas)
# before token comparison so "2021-SFR1" and "BORROWER," compare correctly.
_PUNCT = re.compile(r'[-,;]+')

def _cmp_norm(s):
    return re.sub(r'\s+', ' ', _PUNCT.sub(' ', s)).strip()

DB = dict(host="localhost", port=5434, dbname="who_owns_atl", user="woa", password="woa")


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

SETUP_STMTS = [
    # Normalization function (idempotent)
    r"""
    CREATE OR REPLACE FUNCTION normalize_biz_name(txt TEXT) RETURNS TEXT
    LANGUAGE sql IMMUTABLE STRICT AS $$
        SELECT regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            upper(trim(txt)),
                            '\.', '', 'g'
                        ),
                        '\mL\s+L\s+P\M', 'LLP', 'g'
                    ),
                    '\mL\s+L\s+C\M', 'LLC', 'g'
                ),
                '\mL\s+P\M', 'LP', 'g'
            ),
            '\s+', ' ', 'g'
        )
    $$
    """,
    # Add normalized column to SOS entities
    "ALTER TABLE sos.entities ADD COLUMN IF NOT EXISTS biz_name_norm TEXT",
    """UPDATE sos.entities SET biz_name_norm = normalize_biz_name(business_name)
       WHERE biz_name_norm IS NULL""",
    # btree index for exact match
    """CREATE INDEX IF NOT EXISTS idx_sos_entities_biz_name_norm
       ON sos.entities (biz_name_norm)""",
    # GIN trgm index on normalized name for fuzzy match
    """CREATE INDEX IF NOT EXISTS idx_sos_entities_biz_name_norm_trgm
       ON sos.entities USING gin (biz_name_norm gin_trgm_ops)""",
    # Output table
    "DROP TABLE IF EXISTS sos_matches",
    """CREATE TABLE sos_matches (
        owner_name_norm    TEXT,
        sos_control_number TEXT,
        sos_business_id    TEXT,
        sos_business_name  TEXT,
        sos_biz_name_norm  TEXT,
        match_type         TEXT,
        similarity         FLOAT,
        entity_status      TEXT,
        business_type      TEXT,
        foreign_state      TEXT
    )""",
    # Distinct normalized corporate owner names from both counties
    """CREATE TEMP TABLE _parcel_owners AS
       SELECT DISTINCT normalize_biz_name(owner) AS owner_norm
       FROM fulton_parcels
       WHERE is_corporate = TRUE AND owner IS NOT NULL AND trim(owner) <> ''
       UNION
       SELECT DISTINCT normalize_biz_name(ownernme1)
       FROM dekalb_parcels
       WHERE is_corporate = TRUE AND ownernme1 IS NOT NULL AND trim(ownernme1) <> ''""",
    "DELETE FROM _parcel_owners WHERE owner_norm IS NULL OR owner_norm = ''",
    "CREATE INDEX ON _parcel_owners (owner_norm)",
]

EXACT_MATCH_SQL = """
INSERT INTO sos_matches
SELECT
    po.owner_norm,
    e.control_number,
    e.business_id,
    e.business_name,
    e.biz_name_norm,
    'exact',
    1.0,
    e.entity_status,
    e.business_type_desc,
    e.foreign_state
FROM _parcel_owners po
JOIN sos.entities e ON e.biz_name_norm = po.owner_norm;
"""


STATS_SQL = """
SELECT
    match_type,
    count(*) AS matches,
    round(avg(similarity)::numeric, 3) AS avg_sim
FROM sos_matches
GROUP BY match_type
ORDER BY match_type;
"""

TOTAL_SQL = """
SELECT
    (SELECT count(*) FROM _parcel_owners) AS total_parcel_names,
    (SELECT count(DISTINCT owner_name_norm) FROM sos_matches) AS matched,
    (SELECT count(*) FROM _parcel_owners) -
        (SELECT count(DISTINCT owner_name_norm) FROM sos_matches) AS unmatched;
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(conn, label, sql, threshold=None):
    print(f"\n{label}...")
    cur = conn.cursor()
    if threshold is not None:
        cur.execute(f"SET pg_trgm.similarity_threshold = {threshold}")
    cur.execute(sql)
    rows = cur.rowcount
    conn.commit()
    cur.close()
    if rows >= 0:
        print(f"  {rows:,} rows inserted")
    return rows


def query(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    cur.close()
    return cols, rows


def main():
    conn = psycopg2.connect(**DB)

    print("Setting up: normalization function, normalized column, parcel owner list...")
    cur = conn.cursor()
    for stmt in SETUP_STMTS:
        cur.execute(stmt)
        conn.commit()
    cur.close()
    print("  Setup complete")

    # Count what we're working with
    cols, rows = query(conn, "SELECT count(*) FROM _parcel_owners")
    print(f"  {rows[0][0]:,} distinct normalized corporate owner names to match")

    run(conn, "Step 1: Exact match", EXACT_MATCH_SQL)

    # Fetch unmatched names
    cols, rows = query(conn, """
        SELECT owner_norm FROM _parcel_owners
        WHERE owner_norm NOT IN (SELECT owner_name_norm FROM sos_matches)
        ORDER BY owner_norm
    """)
    unmatched = [r[0] for r in rows]
    print(f"\n  {len(unmatched):,} names unmatched after exact step")

    # Load all SOS names into memory and build a prefix-blocked index.
    # Prefix blocking: group SOS names by first 5 chars so each parcel name
    # only compares against ~10-100 candidates instead of all 4.3M.
    # We also index by first 3 chars as a fallback for transpositions.
    print("  Loading SOS names into memory for prefix-blocked fuzzy matching...")
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (biz_name_norm)
            biz_name_norm, control_number, business_id,
            business_name, entity_status, business_type_desc, foreign_state
        FROM sos.entities
        WHERE biz_name_norm IS NOT NULL AND length(biz_name_norm) >= 4
        ORDER BY biz_name_norm, entity_status
    """)
    sos_rows = cur.fetchall()
    cur.close()
    print(f"  Loaded {len(sos_rows):,} distinct SOS names")

    # Build prefix index: first 5 chars → list of (biz_name_norm, *rest)
    # Build prefix5 index: first 5 chars of biz_name_norm → list of SOS rows
    prefix5 = collections.defaultdict(list)
    for row in sos_rows:
        prefix5[row[0][:5]].append(row)

    def best_match(parcel_name, threshold_high=80, threshold_low=65):
        """Return (best_row, score, match_type) or None using 5-char prefix blocking.

        Both sides further normalized (_cmp_norm) before token_sort_ratio so that
        hyphens/commas don't split tokens differently (e.g. '2021-SFR1' vs '2021 SFR1').
        """
        candidates = prefix5.get(parcel_name[:5], [])
        if not candidates:
            return None, 0, None

        parcel_cmp = _cmp_norm(parcel_name)
        best_score = 0
        best_row = None
        for row in candidates:
            score = fuzz.token_sort_ratio(parcel_cmp, _cmp_norm(row[0]))
            if score > best_score:
                best_score = score
                best_row = row

        if best_score >= threshold_high:
            return best_row, best_score / 100.0, "trgm_high"
        if best_score >= threshold_low:
            return best_row, best_score / 100.0, "trgm_low"
        return None, 0, None

    trgm_high = 0
    trgm_low = 0
    inserts = []
    BATCH = 500

    for i, name in enumerate(unmatched):
        if i % 5000 == 0 and i > 0:
            print(f"  {i:,}/{len(unmatched):,} — high={trgm_high:,} low={trgm_low:,}", flush=True)

        row, score, match_type = best_match(name)
        if match_type is None:
            continue

        inserts.append((
            name, row[1], row[2], row[3], row[0],
            match_type, score,
            row[4], row[5], row[6],
        ))
        if match_type == "trgm_high":
            trgm_high += 1
        else:
            trgm_low += 1

        if len(inserts) >= BATCH:
            cur = conn.cursor()
            cur.executemany(
                "INSERT INTO sos_matches VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                inserts,
            )
            conn.commit()
            cur.close()
            inserts = []

    if inserts:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO sos_matches VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            inserts,
        )
        conn.commit()
        cur.close()

    print(f"  Trigram complete: {trgm_high:,} high, {trgm_low:,} low", flush=True)

    print("\n--- Match summary by type ---")
    cols, rows = query(conn, STATS_SQL)
    print(f"  {'type':<12} {'matches':>8} {'avg_sim':>8}")
    for r in rows:
        print(f"  {r[0]:<12} {r[1]:>8,} {float(r[2]):>8.3f}")

    print("\n--- Overall ---")
    cols, rows = query(conn, TOTAL_SQL)
    r = rows[0]
    total, matched, unmatched = r[0], r[1], r[2]
    pct = matched / total * 100 if total else 0
    print(f"  Total parcel names:  {total:>7,}")
    print(f"  Matched:             {matched:>7,}  ({pct:.1f}%)")
    print(f"  Unmatched:           {unmatched:>7,}")

    # Index the output table
    cur = conn.cursor()
    cur.execute("CREATE INDEX ON sos_matches (owner_name_norm)")
    cur.execute("CREATE INDEX ON sos_matches (sos_control_number)")
    cur.execute("CREATE INDEX ON sos_matches (match_type)")
    conn.commit()
    cur.close()

    print("\nDone. Results in public.sos_matches")
    conn.close()


if __name__ == "__main__":
    main()
