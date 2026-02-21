"""Enrich owner_entities with GA SOS data from sos_matches.

Adds SOS columns to owner_entities:
  sos_control_number, sos_business_id, sos_status, sos_business_type,
  sos_foreign_state, sos_registered_agent, sos_registered_agent_id,
  sos_principal_city, sos_principal_state, sos_match_type, sos_similarity

Only exact and trgm_high matches (similarity >= 0.80) are used.
trgm_low matches are ignored — too many false positives.

Join strategy: normalize_biz_name(oe.owner_name_norm) = sm.owner_name_norm
(catches ~1,600 more entities than direct equality due to period/spacing diffs).
"""

from sqlalchemy import create_engine, text

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

ADD_COLUMNS_SQL = """
ALTER TABLE owner_entities
    ADD COLUMN IF NOT EXISTS sos_control_number    TEXT,
    ADD COLUMN IF NOT EXISTS sos_business_id       TEXT,
    ADD COLUMN IF NOT EXISTS sos_status            TEXT,
    ADD COLUMN IF NOT EXISTS sos_business_type     TEXT,
    ADD COLUMN IF NOT EXISTS sos_foreign_state     TEXT,
    ADD COLUMN IF NOT EXISTS sos_registered_agent  TEXT,
    ADD COLUMN IF NOT EXISTS sos_registered_agent_id TEXT,
    ADD COLUMN IF NOT EXISTS sos_registered_agent_address TEXT,
    ADD COLUMN IF NOT EXISTS sos_principal_city    TEXT,
    ADD COLUMN IF NOT EXISTS sos_principal_state   TEXT,
    ADD COLUMN IF NOT EXISTS sos_match_type        TEXT,
    ADD COLUMN IF NOT EXISTS sos_similarity        FLOAT;
"""

ENRICH_SQL = """
UPDATE owner_entities oe
SET
    sos_control_number     = src.control_number,
    sos_business_id        = src.business_id,
    sos_status             = src.entity_status,
    sos_business_type      = src.business_type_desc,
    sos_foreign_state      = src.foreign_state,
    sos_registered_agent   = src.ra_name,
    sos_registered_agent_id = src.registered_agent_id,
    sos_registered_agent_address = src.ra_addr,
    sos_principal_city     = src.principal_city,
    sos_principal_state    = src.principal_state,
    sos_match_type         = src.match_type,
    sos_similarity         = src.similarity
FROM (
    SELECT DISTINCT ON (sm.owner_name_norm)
        sm.owner_name_norm,
        e.control_number,
        e.business_id,
        e.entity_status,
        e.business_type_desc,
        e.foreign_state,
        e.registered_agent_id,
        ra.name                         AS ra_name,
        ra.line1                        AS ra_addr,
        addr.city                       AS principal_city,
        addr.state                      AS principal_state,
        sm.match_type,
        sm.similarity
    FROM sos_matches sm
    JOIN sos.entities e ON e.control_number = sm.sos_control_number
    LEFT JOIN sos.registered_agents ra
        ON ra.registered_agent_id = e.registered_agent_id
    LEFT JOIN LATERAL (
        SELECT city, state
        FROM sos.addresses a
        WHERE a.control_number = e.control_number
          AND a.city IS NOT NULL AND a.city <> ''
        LIMIT 1
    ) addr ON TRUE
    WHERE sm.match_type IN ('exact', 'trgm_high')
    ORDER BY sm.owner_name_norm, sm.similarity DESC
) src
WHERE normalize_biz_name(oe.owner_name_norm) = src.owner_name_norm;
"""

STATS_SQL = """
SELECT
    sos_match_type,
    count(*)                                                AS entities,
    count(DISTINCT sos_control_number)                      AS distinct_sos_entities,
    count(*) FILTER (WHERE sos_status LIKE 'Active%')       AS active,
    count(*) FILTER (WHERE sos_status LIKE 'Admin%')        AS admin_dissolved,
    count(*) FILTER (WHERE sos_foreign_state IS NOT NULL
                       AND sos_foreign_state <> '')         AS foreign_entities
FROM owner_entities
WHERE sos_match_type IS NOT NULL
GROUP BY sos_match_type
ORDER BY sos_match_type;
"""

FOREIGN_SQL = """
SELECT sos_foreign_state, count(*) AS entities
FROM owner_entities
WHERE sos_foreign_state IS NOT NULL AND sos_foreign_state <> ''
GROUP BY sos_foreign_state
ORDER BY entities DESC
LIMIT 15;
"""

RA_SQL = """
SELECT sos_registered_agent, count(*) AS entities
FROM owner_entities
WHERE sos_registered_agent IS NOT NULL AND sos_registered_agent <> ''
  AND sos_match_type IN ('exact', 'trgm_high')
GROUP BY sos_registered_agent
ORDER BY entities DESC
LIMIT 20;
"""


def main():
    with engine.begin() as conn:
        print("Adding SOS columns to owner_entities...")
        conn.execute(text(ADD_COLUMNS_SQL))
        print("  Done")

        print("\nEnriching owner_entities from sos_matches + sos schema...")
        result = conn.execute(text(ENRICH_SQL))
        print(f"  {result.rowcount:,} owner_entities enriched")

        print("\nAdding indexes on new SOS columns...")
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oe_sos_control ON owner_entities (sos_control_number)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_oe_sos_ra_id ON owner_entities (sos_registered_agent_id)"))
        print("  Done")

    print("\n--- Enrichment summary by match type ---")
    with engine.connect() as conn:
        rows = conn.execute(text(STATS_SQL)).fetchall()
        print(f"  {'type':<12} {'entities':>9} {'sos_uniq':>9} {'active':>8} {'dissolved':>10} {'foreign':>8}")
        for r in rows:
            print(f"  {r[0]:<12} {r[1]:>9,} {r[2]:>9,} {r[3]:>8,} {r[4]:>10,} {r[5]:>8,}")

        print("\n--- Top foreign incorporation states ---")
        rows = conn.execute(text(FOREIGN_SQL)).fetchall()
        for r in rows:
            print(f"  {r[0]:<20} {r[1]:>6,}")

        print("\n--- Top registered agents (non-commercial proxy for control) ---")
        rows = conn.execute(text(RA_SQL)).fetchall()
        for r in rows:
            print(f"  {r[1]:>6,}  {r[0]}")

    print("\nDone.")


if __name__ == "__main__":
    main()
