"""Normalize owner mailing addresses using libpostal for ownership network matching.

Builds a lookup table of distinct raw addresses -> normalized form,
then joins back to parcel tables. Uses concurrent requests for speed.
"""

import concurrent.futures
import requests
from sqlalchemy import create_engine, text

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
LIBPOSTAL_URL = "http://localhost:6789"
WORKERS = 32
BATCH_SIZE = 10000

engine = create_engine(DB_URL)
session = requests.Session()


def parse_address(addr: str) -> str:
    """Parse address via libpostal and return normalized canonical form."""
    if not addr or not addr.strip():
        return ""
    try:
        resp = session.post(f"{LIBPOSTAL_URL}/parser", json={"query": addr}, timeout=5)
        resp.raise_for_status()
        parts = {item["label"]: item["value"] for item in resp.json()}
    except Exception:
        return addr.strip().upper()

    components = []
    for key in ("house_number", "road", "unit", "city", "state", "postcode"):
        if key in parts:
            components.append(parts[key])
    return " ".join(components).upper() if components else addr.strip().upper()


def normalize_batch(addrs: list[str]) -> list[str]:
    """Normalize a batch of addresses concurrently."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(parse_address, addrs))


def setup_lookup_table(engine):
    """Create address normalization lookup table."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS addr_norm_lookup;"))
        conn.execute(text("""
            CREATE TABLE addr_norm_lookup (
                raw_addr TEXT PRIMARY KEY,
                norm_addr TEXT
            );
        """))

        # Insert distinct addresses from both tables
        print("Collecting distinct owner addresses...")
        conn.execute(text("""
            INSERT INTO addr_norm_lookup (raw_addr)
            SELECT DISTINCT TRIM(CONCAT(owneraddr1, ' ', owneraddr2))
            FROM fulton_parcels
            WHERE owneraddr1 IS NOT NULL OR owneraddr2 IS NOT NULL
            ON CONFLICT DO NOTHING;
        """))
        conn.execute(text("""
            INSERT INTO addr_norm_lookup (raw_addr)
            SELECT DISTINCT TRIM(CONCAT(pstladdress, ' ', pstlcitystatezip))
            FROM dekalb_parcels
            WHERE pstladdress IS NOT NULL OR pstlcitystatezip IS NOT NULL
            ON CONFLICT DO NOTHING;
        """))

    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM addr_norm_lookup")).scalar()
        print(f"  {total:,} distinct addresses to normalize")
    return total


def normalize_all(engine, total):
    """Process all addresses in the lookup table."""
    offset = 0
    done = 0
    while offset < total:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT raw_addr FROM addr_norm_lookup
                WHERE norm_addr IS NULL
                ORDER BY raw_addr
                LIMIT :limit
            """), {"limit": BATCH_SIZE}).fetchall()

        if not rows:
            break

        addrs = [r.raw_addr for r in rows]
        norms = normalize_batch(addrs)

        updates = [{"raw": a, "norm": n} for a, n in zip(addrs, norms)]
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE addr_norm_lookup SET norm_addr = :norm WHERE raw_addr = :raw
            """), updates)

        done += len(updates)
        offset += len(updates)
        print(f"  {done:,} / {total:,} ({100*done/total:.1f}%)")


def apply_to_tables(engine):
    """Join normalized addresses back to parcel tables."""
    print("Applying to fulton_parcels...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE fulton_parcels ADD COLUMN IF NOT EXISTS owner_addr_norm TEXT;"))
        result = conn.execute(text("""
            UPDATE fulton_parcels f
            SET owner_addr_norm = l.norm_addr
            FROM addr_norm_lookup l
            WHERE TRIM(CONCAT(f.owneraddr1, ' ', f.owneraddr2)) = l.raw_addr;
        """))
        print(f"  Updated {result.rowcount:,} rows")

    print("Applying to dekalb_parcels...")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE dekalb_parcels ADD COLUMN IF NOT EXISTS owner_addr_norm TEXT;"))
        result = conn.execute(text("""
            UPDATE dekalb_parcels d
            SET owner_addr_norm = l.norm_addr
            FROM addr_norm_lookup l
            WHERE TRIM(CONCAT(d.pstladdress, ' ', d.pstlcitystatezip)) = l.raw_addr;
        """))
        print(f"  Updated {result.rowcount:,} rows")


def update_unified_view(engine):
    """Recreate the unified view to include owner_addr_norm."""
    with engine.begin() as conn:
        conn.execute(text("DROP VIEW IF EXISTS parcels_unified CASCADE;"))
        conn.execute(text("""
            CREATE VIEW parcels_unified AS
            SELECT
                'fulton'::text AS county,
                parcelid AS parcel_id,
                owner AS owner_name,
                NULL::text AS owner_name2,
                address AS site_address,
                owneraddr1 AS owner_address,
                owneraddr2 AS owner_city_state_zip,
                owner_addr_norm,
                classcode AS property_class,
                lucode AS land_use,
                livunits::int AS living_units,
                landacres AS land_acres,
                NULL::numeric AS appraised_value,
                taxdist AS tax_district,
                nbrhood AS neighborhood_code,
                subdiv AS subdivision,
                is_corporate,
                is_institutional,
                geometry
            FROM fulton_parcels
            UNION ALL
            SELECT
                'dekalb'::text AS county,
                COALESCE(parcelid, lowparcelid) AS parcel_id,
                ownernme1 AS owner_name,
                ownernme2 AS owner_name2,
                siteaddress AS site_address,
                pstladdress AS owner_address,
                pstlcitystatezip AS owner_city_state_zip,
                owner_addr_norm,
                classdscrp AS property_class,
                landuse AS land_use,
                NULL::int AS living_units,
                NULL::numeric AS land_acres,
                totapr1 AS appraised_value,
                cvttxdscrp AS tax_district,
                nghbrhdcd AS neighborhood_code,
                cnvyname AS subdivision,
                is_corporate,
                is_institutional,
                geometry
            FROM dekalb_parcels;
        """))
    print("  Updated parcels_unified view")


if __name__ == "__main__":
    total = setup_lookup_table(engine)

    print("\nNormalizing addresses...")
    normalize_all(engine, total)

    print("\nApplying normalized addresses to parcel tables...")
    apply_to_tables(engine)

    print("\nUpdating unified view...")
    update_unified_view(engine)

    # Sample
    with engine.connect() as conn:
        print("\nSample (Fulton corporate):")
        rows = conn.execute(text("""
            SELECT owner, owneraddr1, owneraddr2, owner_addr_norm
            FROM fulton_parcels WHERE is_corporate AND owner_addr_norm != '' LIMIT 5
        """)).fetchall()
        for r in rows:
            print(f"  {r.owner.strip()} | {r.owneraddr1} {r.owneraddr2} -> {r.owner_addr_norm}")

    print("\nDone.")
