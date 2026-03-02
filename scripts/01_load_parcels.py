"""Load Fulton + DeKalb county parcel GeoJSON into PostGIS, then create a unified view."""

import geopandas as gpd
from sqlalchemy import create_engine, text
import argparse

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
DATA_DIR = "data/json/geojson/latest"

engine = create_engine(DB_URL)


def load_fulton(engine):
    print("Loading Fulton County parcels...")
    gdf = gpd.read_file(f"{DATA_DIR}/Fulton_County_Tax_Parcel.json")
    print(f"  {len(gdf)} features read")

    # Normalize column names to lowercase
    gdf.columns = [c.lower() for c in gdf.columns]
    gdf = gdf.to_crs(epsg=4326)

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS fulton_parcels CASCADE;"))

    gdf.to_postgis("fulton_parcels", engine, if_exists="replace", index=False)
    print(f"  Loaded into fulton_parcels")


def load_dekalb(engine):
    print("Loading DeKalb County parcels...")
    gdf = gpd.read_file(f"{DATA_DIR}/Dekalb_County_Tax_Parcels.geojson")
    print(f"  {len(gdf)} features read")

    gdf.columns = [c.lower() for c in gdf.columns]
    gdf = gdf.to_crs(epsg=4326)

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS dekalb_parcels CASCADE;"))

    gdf.to_postgis("dekalb_parcels", engine, if_exists="replace", index=False)
    print(f"  Loaded into dekalb_parcels")


def create_unified_view(engine):
    """Create a unified parcels view with common columns from both counties.
    FILTERED to residential properties only (R*, T*, or C* with living units).
    """
    print("Creating unified parcels view (residential focus)...")
    with engine.begin() as conn:
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
                    (lucode IN ('106', '110') OR (COALESCE(livunits::int, 0) = 0 AND (subdiv ILIKE '%%CONDO%%' OR subdiv ILIKE '%%CONDOMINIUM%%') AND lucode NOT IN ('111', '166', '188', '208')))
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
                lucode IN ('111', '166', '188')

            UNION ALL

            SELECT
                'dekalb' AS county,
                COALESCE(parcelid, lowparcelid) AS parcel_id,
                ownernme1 AS owner_name,
                ownernme2 AS owner_name2,
                siteaddress AS site_address,
                pstladdress AS owner_address,
                pstlcitystatezip AS owner_city_state_zip,
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
                (0)::int AS is_condo_potential,
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


def create_indexes(engine):
    print("Creating indexes...")
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_fulton_geom ON fulton_parcels USING GIST (geometry);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_dekalb_geom ON dekalb_parcels USING GIST (geometry);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_fulton_owner ON fulton_parcels (owner);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_dekalb_owner ON dekalb_parcels (ownernme1);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_fulton_parcelid ON fulton_parcels (parcelid);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_dekalb_parcelid ON dekalb_parcels (parcelid);"))
    print("  Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--create-view", action="store_true", help="ONLY create unified view (requires flags exist)")
    parser.add_argument("--load-only", action="store_true", help="ONLY load raw data and index (skips view)")
    args = parser.parse_args()

    if args.create_view:
        create_unified_view(engine)
    elif args.load_only:
        load_fulton(engine)
        load_dekalb(engine)
        create_indexes(engine)
    else:
        # Default behavior: load and index (no view)
        load_fulton(engine)
        load_dekalb(engine)
        create_indexes(engine)
    
    print("\nAll done. Verifying counts:")
    with engine.connect() as conn:
        tables = ["fulton_parcels", "dekalb_parcels"]
        if args.create_view:
            tables.append("parcels_unified")
        for tbl in tables:
            try:
                n = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
                print(f"  {tbl}: {n:,}")
            except Exception:
                print(f"  {tbl}: (could not count)")
