"""Enrich county parcels with Atlanta city attributes via spatial join.

Adds three columns to fulton_parcels and dekalb_parcels:
  city_neighborhood  TEXT  -- e.g. "Midtown", "Kirkwood"
  city_npu           TEXT  -- Neighborhood Planning Unit (A-Z)
  city_council       TEXT  -- City council district number

Source: gis.Tax_Parcel (171K city parcels with pre-coded NEIGHBORHOOD/NPU/COUNCIL).

Strategy: centroid-based lateral join picking the smallest matching city parcel
(most specific — handles condo-unit level city data that produces 100s of matches
for a single county parcel, all with identical neighborhood/NPU/council values).

Parcels outside Atlanta city limits get NULLs — expected for most of Fulton/DeKalb.
"""

from sqlalchemy import create_engine, text

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

ADD_COLUMNS_SQL = """
ALTER TABLE fulton_parcels
    ADD COLUMN IF NOT EXISTS city_neighborhood TEXT,
    ADD COLUMN IF NOT EXISTS city_npu          TEXT,
    ADD COLUMN IF NOT EXISTS city_council      TEXT;

ALTER TABLE dekalb_parcels
    ADD COLUMN IF NOT EXISTS city_neighborhood TEXT,
    ADD COLUMN IF NOT EXISTS city_npu          TEXT,
    ADD COLUMN IF NOT EXISTS city_council      TEXT;
"""

# DISTINCT ON picks the smallest city Tax_Parcel polygon (most specific match).
# Uses ctid as a stable row identifier for the UPDATE join.
# Condo-unit level city data produces 100s of matches per county parcel,
# but they all agree on NEIGHBORHOOD/NPU/COUNCIL — smallest is cleanest.
UPDATE_FULTON_SQL = """
UPDATE fulton_parcels f
SET
    city_neighborhood = src.neighborhood,
    city_npu          = src.npu,
    city_council      = src.council
FROM (
    SELECT DISTINCT ON (f.ctid)
        f.ctid,
        tp."NEIGHBORHOOD" AS neighborhood,
        tp."NPU"          AS npu,
        tp."COUNCIL"      AS council
    FROM fulton_parcels f
    JOIN gis."Tax_Parcel" tp
      ON ST_Intersects(ST_Centroid(f.geometry), tp.geometry)
    ORDER BY f.ctid, ST_Area(tp.geometry)
) src
WHERE f.ctid = src.ctid;
"""

UPDATE_DEKALB_SQL = """
UPDATE dekalb_parcels d
SET
    city_neighborhood = src.neighborhood,
    city_npu          = src.npu,
    city_council      = src.council
FROM (
    SELECT DISTINCT ON (d.ctid)
        d.ctid,
        tp."NEIGHBORHOOD" AS neighborhood,
        tp."NPU"          AS npu,
        tp."COUNCIL"      AS council
    FROM dekalb_parcels d
    JOIN gis."Tax_Parcel" tp
      ON ST_Intersects(ST_Centroid(d.geometry), tp.geometry)
    ORDER BY d.ctid, ST_Area(tp.geometry)
) src
WHERE d.ctid = src.ctid;
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_fulton_city_npu  ON fulton_parcels (city_npu);
CREATE INDEX IF NOT EXISTS idx_dekalb_city_npu  ON dekalb_parcels (city_npu);
"""

STATS_SQL = """
SELECT
    'fulton'  AS county,
    count(*)  AS total,
    count(*) FILTER (WHERE city_council IS NOT NULL) AS in_city,
    count(*) FILTER (WHERE city_neighborhood IS NOT NULL AND city_neighborhood <> '') AS has_neighborhood,
    count(*) FILTER (WHERE city_npu IS NOT NULL AND city_npu <> '') AS has_npu
FROM fulton_parcels

UNION ALL

SELECT
    'dekalb'  AS county,
    count(*)  AS total,
    count(*) FILTER (WHERE city_council IS NOT NULL) AS in_city,
    count(*) FILTER (WHERE city_neighborhood IS NOT NULL AND city_neighborhood <> '') AS has_neighborhood,
    count(*) FILTER (WHERE city_npu IS NOT NULL AND city_npu <> '') AS has_npu
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
ORDER BY city_council::int;
"""


def main():
    with engine.begin() as conn:
        print("Adding city enrichment columns to county tables...")
        conn.execute(text(ADD_COLUMNS_SQL))
        print("  Done")

        print("\nUpdating Fulton parcels via spatial join (may take a few minutes)...")
        result = conn.execute(text(UPDATE_FULTON_SQL))
        print(f"  {result.rowcount:,} Fulton parcels enriched")

        print("\nUpdating DeKalb parcels via spatial join...")
        result = conn.execute(text(UPDATE_DEKALB_SQL))
        print(f"  {result.rowcount:,} DeKalb parcels enriched")

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
