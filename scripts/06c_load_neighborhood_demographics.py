"""Load Neighborhood Demographic data into the gis schema.

Source: Official_Neighborhoods_with_Current_Demographic_Data_(2024).geojson
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
FILE_PATH = SOURCES["atlanta_gis_neighborhoods_demographics"]["file_path"]

# Mapping of cryptic GeoJSON fields to readable database columns
# Based on nbh_demo_field_list.html analysis
FIELD_MAP = {
    "NAME": "neighborhood_name",
    "populati_1": "total_population",
    "gender_MED": "median_age",
    "householdt": "total_households",
    "OwnerRente": "owner_occupied_count",
    "OwnerRen_1": "owner_occupied_pct",
    "OwnerRen_2": "renter_occupied_count",
    "OwnerRen_3": "renter_occupied_pct",
    "housinguni": "total_housing_units",
    "vacant_VAC": "vacant_units_count",
    "vacant_V_1": "vacant_units_pct",
    "raceandh_1": "white_pct",
    "raceandh_3": "black_pct",
    "raceandh_5": "asian_pct",
    "hispanic_1": "hispanic_pct",
    "households": "below_poverty_count",
    "householdi": "median_household_income",
    "homevalue_": "median_home_value",
    "educatio_5": "bachelors_degree_pct",
    "educatio_6": "graduate_degree_pct",
    "househol_1": "avg_household_size"
}

def load_demographics():
    print(f"Reading {FILE_PATH}...")
    t0 = time.time()
    gdf = gpd.read_file(FILE_PATH)
    print(f"  Read {len(gdf)} features in {time.time()-t0:.1f}s")

    # Rename and keep necessary columns
    # We keep geometry and NPU
    rename_dict = {k: v for k, v in FIELD_MAP.items() if k in gdf.columns}
    cols_to_keep = list(rename_dict.keys()) + ["geometry", "NPU"]
    gdf = gdf[cols_to_keep].rename(columns=rename_dict)

    # Ensure CRS is 4326
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        print(f"  Converting CRS from {gdf.crs.to_epsg()} to 4326...")
        gdf = gdf.to_crs(epsg=4326)

    print(f"Loading into gis.neighborhood_demographics...")
    t1 = time.time()
    engine = create_engine(DB_URL)
    gdf.to_postgis("neighborhood_demographics", engine, schema="gis", if_exists="replace", index=False)
    print(f"  Loaded in {time.time()-t1:.1f}s")

    # Create spatial index and a name index
    with engine.connect() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_nbhd_demo_geom ON gis.neighborhood_demographics USING GIST(geometry)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_nbhd_demo_name ON gis.neighborhood_demographics (neighborhood_name)"))
        conn.commit()
    print("  Indexes created.")

if __name__ == "__main__":
    if not os.path.exists(FILE_PATH):
        print(f"Error: File not found at {FILE_PATH}")
        sys.exit(1)
    
    load_demographics()
    print("\nDone.")
