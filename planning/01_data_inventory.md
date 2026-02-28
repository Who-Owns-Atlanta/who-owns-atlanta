# Data Inventory

## Primary Parcel Datasets

We use **two county-level datasets** as primary sources. The City of Atlanta `Tax_Parcel.json`
is NOT used directly — both counties fully cover Atlanta's city limits, and city-level detail
(council district, NPU, neighborhood) can be linked back via ParcelID/geometry later.

### 1. Fulton County Tax Parcels — `Fulton_County_Tax_Parcel.json` (457MB)
- **Source:** Fulton County GIS
- **28 fields**
- **Key fields:**
  - `ParcelID` / `FeatureID` — parcel identifier
  - `Address`, `AddrNumber`, `AddrPreDir`, `AddrStreet`, `AddrSuffix`, `AddrPosDir`, `AddrUntTyp`, `AddrUnit` — property address (well-parsed)
  - `Owner` — single owner name field
  - `OwnerAddr1`, `OwnerAddr2` — owner mailing address (line 1 + city/state/zip on line 2)
  - `ClassCode` — property class (e.g., "R4")
  - `ExCode` — exemption code
  - `LUCode` — land use code
  - `LivUnits` — living units
  - `LandAcres` — lot size
  - `TaxDist`, `NbrHood`, `Subdiv` — location references
  - `TaxYear` — 2026
- **Geometry:** Polygon (CRS84)

### 2. DeKalb County Tax Parcels — `Dekalb_County_Tax_Parcels.geojson` (451MB)
- **Source:** DeKalb County GIS
- **63 fields** — most fields, but sparsest data (many nulls observed in sample)
- **Key fields:**
  - `PARCELID` / `LOWPARCELID` / `PRCLKEY` — parcel identifiers
  - `SITEADDRESS`, `ADDRESS_NUMBER`, `FULL_STREET_NAME`, `CITY`, `STATE`, `ZIP`, `UNIT_NO`, `UNIT_TYPE` — property address
  - `OWNERNME1`, `OWNERNME2` — owner names
  - `PSTLADDRESS`, `PSTLCITY`, `PSTLSTATE`, `PSTLZIP5`, `PSTLZIP4`, `PSTLCITYSTATEZIP` — owner mailing
  - `CVTTXDSCRP` — tax district description (e.g., "ATLANTA")
  - `CLASSDSCRP` — property class
  - `BUILDING`, `UNIT` — building/unit identifiers (condos)
  - `CNVYNAME` — subdivision name
  - `SUBDIVISION_TYPE` — e.g., "CONDOS"
  - `TOTAPR1` — total appraised value
  - `ZONING`, `LANDUSE` — zoning/land use
- **Geometry:** Polygon (EPSG:4326)
- **Note:** First feature sampled had many null core fields — data quality may vary by record

## Schema Alignment (Fulton vs DeKalb)

| Concept | Fulton | DeKalb |
|---------|--------|--------|
| Parcel ID | `ParcelID` | `PARCELID` |
| Owner Name | `Owner` (single field) | `OWNERNME1` + `OWNERNME2` |
| Owner Address | `OwnerAddr1` + `OwnerAddr2` | `PSTLADDRESS` + `PSTLCITYSTATEZIP` |
| Property Class | `ClassCode` | `CLASSDSCRP` |
| Exemption | `ExCode` | _(not present)_ |
| Site Address | `Address` (composite) | `SITEADDRESS` |
| Site Addr Components | `AddrNumber`/`AddrStreet`/`AddrSuffix`/`AddrPosDir`/`AddrUnit` | `ADDRESS_NUMBER`/`FULL_STREET_NAME`/`CITY`/`STATE`/`ZIP`/`UNIT_NO` |
| Tax Year | `TaxYear` (2026) | _(not present in sample)_ |
| Lot Size | `LandAcres` | _(not present)_ |
| Appraised Value | _(not present)_ | `TOTAPR1` |

## Reserved / Linkage Dataset

### City of Atlanta Tax Parcels — `Tax_Parcel.json` (332MB)
- **NOT a primary source** — Atlanta is covered by Fulton + DeKalb
- **Use later** for enrichment: link via `PARCELID` or spatial join to get `COUNCIL`, `NPU`, `NEIGHBORHOOD`
- 53 fields including political/geographic assignments not present in county data

## Reference / Overlay Datasets

| File | Size | Purpose |
|------|------|---------|
| `Address_Point.json` | 369MB | Canonical address points (45 fields, parsed components) |
| `Atlanta_City_Limits.json` | 192KB | City boundary polygon — for filtering parcels within Atlanta |
| `Neighborhood.json` | 765KB | Neighborhood boundaries + names + NPU mapping |
| `NPU.json` | 502KB | NPU (Neighborhood Planning Unit) boundaries |
| `Official_City_Council_Districts.geojson` | 503KB | Council district boundaries |
| `Zoning_District.json` | 6MB | Zoning district boundaries |
