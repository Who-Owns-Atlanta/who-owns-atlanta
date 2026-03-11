"""Enrich county parcels with Atlanta city attributes via spatial join.

Adds three columns to fulton_parcels and dekalb_parcels:
  city_neighborhood  TEXT  -- e.g. "Midtown", "Kirkwood"
  city_npu           TEXT  -- Neighborhood Planning Unit (A-Z)
  city_council       TEXT  -- City council district number

Sources: gis.neighborhoods, gis.npu, gis.council_districts.
(Small authoritative layers loaded via scripts/06b_load_city_gis.py)

Parcels outside Atlanta city limits get NULLs — expected for most of Fulton/DeKalb.
"""

from sqlalchemy import create_engine, text
import os

DB_URL = os.environ.get("DATABASE_URL", "postgresql://woa:woa@localhost:5434/who_owns_atl")
engine = create_engine(DB_URL)

ADD_COLUMNS_SQL = """
ALTER TABLE fulton_parcels
    ADD COLUMN IF NOT EXISTS city_neighborhood TEXT,
    ADD COLUMN IF NOT EXISTS city_npu          TEXT,
    ADD COLUMN IF NOT EXISTS city_council      TEXT,
    ADD COLUMN IF NOT EXISTS city_zoning       TEXT;

ALTER TABLE dekalb_parcels
    ADD COLUMN IF NOT EXISTS city_neighborhood TEXT,
    ADD COLUMN IF NOT EXISTS city_npu          TEXT,
    ADD COLUMN IF NOT EXISTS city_council      TEXT,
    ADD COLUMN IF NOT EXISTS city_zoning       TEXT;
"""

# Separate updates for each attribute to ensure robustness.
# Centroid-based join is used for speed and to avoid sliver overlaps.

def get_update_sql(table_name):
    return [
        f"""
        UPDATE {table_name} f
        SET city_neighborhood = n."NAME"
        FROM gis.neighborhoods n
        WHERE ST_Intersects(ST_Centroid(f.geometry), n.geometry);
        """,
        f"""
        UPDATE {table_name} f
        SET city_npu = n."NAME"
        FROM gis.npu n
        WHERE ST_Intersects(ST_Centroid(f.geometry), n.geometry);
        """,
        f"""
        UPDATE {table_name} f
        SET city_council = c."NAME"
        FROM gis.council_districts c
        WHERE ST_Intersects(ST_Centroid(f.geometry), c.geometry);
        """,
        f"""
        UPDATE {table_name} f
        SET city_zoning = z.zoning
        FROM gis.zoning_districts z
        WHERE ST_Intersects(ST_Centroid(f.geometry), z.geometry);
        """
    ]

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_fulton_city_npu  ON fulton_parcels (city_npu);
CREATE INDEX IF NOT EXISTS idx_dekalb_city_npu  ON dekalb_parcels (city_npu);
CREATE INDEX IF NOT EXISTS idx_fulton_city_council ON fulton_parcels (city_council);
CREATE INDEX IF NOT EXISTS idx_dekalb_city_council ON dekalb_parcels (city_council);
"""

STATS_SQL = """
SELECT
    'fulton'  AS county,
    count(*)  AS total,
    count(*) FILTER (WHERE city_council IS NOT NULL) AS in_city,
    count(*) FILTER (WHERE city_neighborhood IS NOT NULL AND city_neighborhood <> '') AS has_neighborhood,
    count(*) FILTER (WHERE city_npu IS NOT NULL AND city_npu <> '') AS has_npu,
    count(*) FILTER (WHERE city_zoning IS NOT NULL AND city_zoning <> '') AS has_zoning
FROM fulton_parcels

UNION ALL

SELECT
    'dekalb'  AS county,
    count(*)  AS total,
    count(*) FILTER (WHERE city_council IS NOT NULL) AS in_city,
    count(*) FILTER (WHERE city_neighborhood IS NOT NULL AND city_neighborhood <> '') AS has_neighborhood,
    count(*) FILTER (WHERE city_npu IS NOT NULL AND city_npu <> '') AS has_npu,
    count(*) FILTER (WHERE city_zoning IS NOT NULL AND city_zoning <> '') AS has_zoning
FROM dekalb_parcels;
"""

NEIGHBORHOOD_SQL = """
SELECT city_neighborhood, count(*) AS parcels
FROM (
    SELECT city_neighborhood FROM fulton_parcels WHERE city_neighborhood IS NOT NULL AND city_neighborhood <> ''
    UNION ALL
    SELECT city_neighborhood FROM dekalb_parcels WHERE city_neighborhood IS NOT NULL AND city_neighborhood <> ''
) combined
GROUP BY city_neighborhood
ORDER BY parcels DESC
LIMIT 20;
"""

NPU_SQL = """
SELECT city_npu, count(*) AS parcels
FROM (
    SELECT city_npu FROM fulton_parcels WHERE city_npu IS NOT NULL AND city_npu <> ''
    UNION ALL
    SELECT city_npu FROM dekalb_parcels WHERE city_npu IS NOT NULL AND city_npu <> ''
) combined
GROUP BY city_npu
ORDER BY city_npu;
"""

COUNCIL_SQL = """
SELECT city_council, count(*) AS parcels
FROM (
    SELECT city_council FROM fulton_parcels WHERE city_council IS NOT NULL AND city_council <> ''
    UNION ALL
    SELECT city_council FROM dekalb_parcels WHERE city_council IS NOT NULL AND city_council <> ''
) combined
GROUP BY city_council
ORDER BY CASE WHEN city_council ~ '^[0-9]+$' THEN city_council::int ELSE 999 END;
"""


def main():
    with engine.begin() as conn:
        print("Adding city enrichment columns to county tables...")
        conn.execute(text(ADD_COLUMNS_SQL))
        print("  Done")

        for table in ["fulton_parcels", "dekalb_parcels"]:
            print(f"\nUpdating {table} via spatial joins...")
            sqls = get_update_sql(table)
            for sql in sqls:
                result = conn.execute(text(sql))
                print(f"  {result.rowcount:,} rows updated")

        print("\nCreating indexes...")
        conn.execute(text(INDEX_SQL))
        print("  Done")

    print("\n--- Enrichment summary ---")
    with engine.connect() as conn:
        rows = conn.execute(text(STATS_SQL)).fetchall()
        print(f"  {'county':<8} {'total':>8} {'in_city':>8} {'w/hood':>8} {'w/npu':>7}")
        for r in rows:
            print(f"  {r[0]:<8} {r[1]:>8,} {r[2]:>8,} {r[3]:>8,} {r[4]:>7,}")

        print("\n--- Top 20 neighborhoods by parcel count ---")
        rows = conn.execute(text(NEIGHBORHOOD_SQL)).fetchall()
        for r in rows:
            print(f"  {r[1]:>6,}  {r[0]}")

        print("\n--- Parcels by NPU ---")
        rows = conn.execute(text(NPU_SQL)).fetchall()
        for r in rows:
            print(f"  NPU {r[0]}: {r[1]:,}")

        print("\n--- Parcels by council district ---")
        rows = conn.execute(text(COUNCIL_SQL)).fetchall()
        for r in rows:
            print(f"  District {r[0]}: {r[1]:,}")

    print("\nDone.")


if __name__ == "__main__":
    main()
