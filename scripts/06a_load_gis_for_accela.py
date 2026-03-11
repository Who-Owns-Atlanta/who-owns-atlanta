"""Load Address_Point and Tax_Parcel GIS data into the gis schema.

These are needed for the geometry-matching trigger on application.records.
- Address_Point: primary match by street address
- Tax_Parcel: fallback match by parcel number (bridges Accela parcel IDs to county data)
"""

import json
import geopandas as gpd
from pathlib import Path
from sqlalchemy import create_engine, text
import sys
import time

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"

def _load_sources():
    root = Path(__file__).resolve().parent.parent
    return json.load(open(root / "web/frontend/data/datasources.json"))

SOURCES = _load_sources()

engine = create_engine(DB_URL)


def load_address_points():
    print("Loading Address_Point.json (~387MB)...")
    t0 = time.time()
    gdf = gpd.read_file(SOURCES["atlanta_address_point"]["file_path"])
    print(f"  Read {len(gdf)} address points in {time.time()-t0:.0f}s")

    # Ensure EPSG:4326
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    t1 = time.time()
    gdf.to_postgis("Address_Point", engine, schema="gis", if_exists="replace", index=False)
    print(f"  Loaded to gis.\"Address_Point\" in {time.time()-t1:.0f}s")

    # Create indexes
    with engine.connect() as conn:
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_address_point_geom ON gis."Address_Point" USING GIST(geometry)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_address_point_addrnum ON gis."Address_Point"("ADDRNUM")'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_address_point_addr_sn ON gis."Address_Point"("ADDR_SN")'))
        conn.commit()
    print("  Indexes created.")


def load_tax_parcels():
    print("Loading Tax_Parcel.json (~348MB)...")
    t0 = time.time()
    gdf = gpd.read_file(SOURCES["atlanta_tax_parcel"]["file_path"])
    print(f"  Read {len(gdf)} tax parcels in {time.time()-t0:.0f}s")

    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    t1 = time.time()
    gdf.to_postgis("Tax_Parcel", engine, schema="gis", if_exists="replace", index=False)
    print(f"  Loaded to gis.\"Tax_Parcel\" in {time.time()-t1:.0f}s")

    # Create indexes
    with engine.connect() as conn:
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_tax_parcel_geom ON gis."Tax_Parcel" USING GIST(geometry)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_tax_parcel_lowparcelid ON gis."Tax_Parcel"("LOWPARCELID")'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS idx_tax_parcel_parcelid ON gis."Tax_Parcel"("PARCELID")'))
        conn.commit()
    print("  Indexes created.")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "all"

    if target in ("all", "address"):
        load_address_points()
    if target in ("all", "parcel"):
        load_tax_parcels()

    print("\nDone. GIS data loaded.")
