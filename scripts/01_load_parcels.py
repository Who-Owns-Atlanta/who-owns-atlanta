"""Load Fulton + DeKalb county parcel GeoJSON into PostGIS, then create a unified view."""

import geopandas as gpd
from sqlalchemy import create_engine, text
import argparse
from scripts.utils import DB_URL, create_unified_view
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
    parser.add_argument("--refresh-mviews", action="store_true", help="Recreate materialized views after view update")
    args = parser.parse_args()

    if args.create_view:
        create_unified_view(engine, refresh_mviews=args.refresh_mviews)
    elif args.load_only:
        load_fulton(engine)
        load_dekalb(engine)
        create_indexes(engine)
    else:
        # Default behavior: load and index (no view)
        load_fulton(engine)
        load_dekalb(engine)
        create_indexes(engine)
        create_unified_view(engine, refresh_mviews=args.refresh_mviews)
    
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
