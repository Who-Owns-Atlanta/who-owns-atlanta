"""Load small City GIS layers (Council, Neighborhood, NPU) into the gis schema.

Authoritative sources:
- Official_City_Council_Districts.geojson
- Neighborhood.json
- NPU.json
"""

import json
import geopandas as gpd
from pathlib import Path
from sqlalchemy import create_engine, text
import sys
import time
import os

DB_URL = os.environ.get("DATABASE_URL", "postgresql://woa:woa@localhost:5434/who_owns_atl")

def _load_sources():
    root = Path(__file__).resolve().parent.parent
    return json.load(open(root / "web/frontend/data/datasources.json"))

SOURCES = _load_sources()

engine = create_engine(DB_URL)

def load_layer(path, table_name):
    print(f'Loading {Path(path).name} into gis."{table_name}"...')
    t0 = time.time()
    gdf = gpd.read_file(path)
    print(f"  Read {len(gdf)} features in {time.time()-t0:.0f}s")

    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    t1 = time.time()
    gdf.to_postgis(table_name, engine, schema="gis", if_exists="replace", index=False)
    print(f'  Loaded to gis."{table_name}" in {time.time()-t1:.0f}s')

    # Create spatial index
    with engine.connect() as conn:
        conn.execute(text(f'CREATE INDEX IF NOT EXISTS idx_{table_name}_geom ON gis."{table_name}" USING GIST(geometry)'))
        conn.commit()
    print("  Spatial index created.")

if __name__ == "__main__":
    load_layer(SOURCES["atlanta_gis_council"]["file_path"], "council_districts")
    load_layer(SOURCES["atlanta_gis_neighborhoods"]["file_path"], "neighborhoods")
    load_layer(SOURCES["atlanta_gis_npu"]["file_path"], "npu")

    print("\nDone. City GIS layers loaded.")
