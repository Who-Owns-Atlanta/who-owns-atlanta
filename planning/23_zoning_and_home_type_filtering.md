# Implementation Log: Zoning and Home Type Filtering

**Date:** 2026-03-10
**Status:** Completed

## Objective
Add an "Official Zoning Districts" layer from the City of Atlanta and use it to provide a "Home Type" filter (e.g., Single-Family vs. Multi-Family) to address concerns about corporate ownership of single-family homes.

## Changes

### 1. Data Sourcing
- Downloaded `Official_Zoning_Districts.geojson` (City of Atlanta Open Data).
- Moved to `data/json/geojson/2026-03-10/Official_Zoning_Districts.geojson`.
- Updated `web/frontend/data/datasources.json` with SHA256 and metadata.

### 2. Database Enrichment
- Created `scripts/06d_load_zoning.py` to load zoning polygons into `gis.zoning_districts`.
- Updated `scripts/11_city_enrichment.py` to spatial join `city_zoning` to both `fulton_parcels` and `dekalb_parcels`.
- Updated `scripts/utils.py` to add `home_type` classification logic to the `parcels_unified` view:
    - **Single-Family:** Atlanta R1-R5 districts, Fulton LU 101/107/110, or DeKalb R3 with SUB/TN/TC landuse.
    - **Multi-Family / Condo:** Atlanta RG/MR districts, Fulton LU 106/211/212, or DeKalb R9/CRC/NC.
    - **Other:** Commercial, Industrial, and mixed-use districts.
- Precedence: City of Atlanta zoning takes absolute precedence over county land use codes for properties within city limits.

### 3. Vector Tiles
- Updated `scripts/build_tiles.sh` to include the `home_type` string attribute in all zoom levels (z10-z14).
- Verified tile sizes (total ~203MB, individual tiles < 1MB).

### 4. Web Interface
- **Filter Panel:** Added "Home Type" dropdown to the filter panel in `index.html`.
- **Map Logic:** Implemented `updateHomeTypeFilter()` in `app.js` using `map.setFilter()` for instant client-side filtering.
- **Detail Panel:** Added "Home type" and "Zoning (City)" rows to the City of Atlanta GIS section of the parcel detail panel.

## Verification
- Zoning data loaded: 2,959 districts.
- Parcels enriched: ~171,000 parcels in city limits now have `city_zoning` and `home_type`.
- Vector tiles successfully built and installed to `/var/www/who-owns-atlanta/tiles/`.
