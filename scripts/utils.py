from sqlalchemy import create_engine, text
import subprocess

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"

def create_unified_view(engine, refresh_mviews=False):
    """Create a unified parcels view with common columns from both counties.
    FILTERED to residential properties only (R*, T*, or C* with living units).
    """
    print("Creating unified parcels view (residential focus)...")
    with engine.begin() as conn:
        # Ensure owner_addr_norm exists so the view doesn't fail if normalization hasn't run
        conn.execute(text("ALTER TABLE fulton_parcels ADD COLUMN IF NOT EXISTS owner_addr_norm TEXT;"))
        conn.execute(text("ALTER TABLE dekalb_parcels ADD COLUMN IF NOT EXISTS owner_addr_norm TEXT;"))
        
        conn.execute(text("DROP VIEW IF EXISTS parcels_unified CASCADE;"))
        conn.execute(text("""
            CREATE VIEW parcels_unified AS

            SELECT
                'fulton' AS county,
                parcelid AS parcel_id,
                owner AS owner_name,
                NULL AS owner_name2,
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
                COALESCE(
                    (lucode IN ('106', '107', '110') OR (COALESCE(livunits::int, 0) = 0 AND (subdiv ILIKE '%%CONDO%%' OR subdiv ILIKE '%%CONDOMINIUM%%') AND lucode NOT IN ('111', '166', '188', '208')))
                , false)::int AS is_condo_potential,
                city_neighborhood,
                city_npu,
                city_council,
                geometry
            FROM fulton_parcels
            WHERE 
                classcode LIKE 'R%%' OR 
                classcode LIKE 'T%%' OR 
                (classcode LIKE 'C%%' AND livunits::int > 0) OR
                lucode IN ('102', '103', '111', '166', '188', '211')

            UNION ALL

            SELECT
                'dekalb' AS county,
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
                (CASE 
                    WHEN cnvyname ILIKE '%%CONDO%%' OR cnvyname ILIKE '%%CONDOMINIUM%%' THEN 1
                    WHEN unit IS NOT NULL AND unit <> '' THEN 1
                    WHEN unit_no IS NOT NULL AND unit_no <> '' THEN 1
                    ELSE 0
                END)::int AS is_condo_potential,
                city_neighborhood,
                city_npu,
                city_council,
                geometry
            FROM dekalb_parcels
            WHERE 
                classdscrp LIKE 'R%%' OR 
                classdscrp LIKE 'T%%' OR 
                (classdscrp LIKE 'C%%' AND 1=1) OR
                classdscrp = 'R9' OR 
                landuse = 'COS' OR 
                common_area IS NOT NULL
        """))
    print("  Created parcels_unified view")

    if refresh_mviews:
        print("\nRefreshing materialized views (required after view cascade)...")
        try:
            # Assumes environment variables or .env are set or use script defaults
            cmd = [
                "psql", "-h", "localhost", "-p", "5434", "-U", "woa", "-d", "who_owns_atl",
                "-f", "scripts/sql/04_create_materialized_views.sql"
            ]
            import os
            env = os.environ.copy()
            env["PGPASSWORD"] = "woa"
            subprocess.run(cmd, env=env, check=True)
            print("  Materialized views refreshed.")
        except Exception as e:
            print(f"  Error refreshing materialized views: {e}")
