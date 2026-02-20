# What If: Expanding to the Five-County Metro Area

**Status:** Research / Exploration
**Context:** Based on *Horizontal Holdings* (@docs/horizontal-holdings.pdf)

---

## Objective

Explore the feasibility and ramifications of expanding "Who Owns Atlanta?" from its current two-county focus (Fulton, DeKalb) to the full "five core counties" mentioned in the research: **Gwinnett, Cobb, and Clayton**.

## 1. Data Sources (Real Parcel Data)

To achieve the same level of granularity, we would need to pull GeoJSON/Shapefile data from these authoritative GIS portals:

| County | GIS Open Data Portal | Primary Layer Name |
|---|---|---|
| **Gwinnett** | [data-gwinnett.opendata.arcgis.com](https://data-gwinnett.opendata.arcgis.com/) | Tax Parcels |
| **Cobb** | [gis-cobbcounty.opendata.arcgis.com](https://gis-cobbcounty.opendata.arcgis.com/) | Parcels |
| **Clayton** | [clayton-county-gis-claytongis.hub.arcgis.com](https://clayton-county-gis-claytongis.hub.arcgis.com/) | Parcels |

## 2. Technical Ramifications

### Data Volume & Scaling
*   **Parcel Count:** Currently ~616k. Adding these three would push the total to **~1.2 Million parcels**. 
*   **Database:** PostgreSQL/PostGIS handles this easily, but materialized views for the map tiles (Phase 3 of the Web Plan) would grow in disk size (est. 4-5GB total).
*   **SOS Matching:** We would likely identify ~30k-50k new "Corporate" names from these counties. Our parallelized hybrid matcher (`scripts/08`) would take an additional **90-120 minutes** to process these.

### The "Permit Gap" (Accela)
This is the most significant functional ramification:
*   **The Issue:** Our current `application.records` data comes from the **City of Atlanta Accela portal**. While this covers parts of Fulton and DeKalb, it does **not** cover Gwinnett, Cobb, or Clayton.
*   **The Impact:** In the UI, a user might look up an Invitation Homes property in Gwinnett and see 0 complaints, while a similar property in Atlanta shows 5. This creates a false sense of "quality" in the suburbs simply due to missing data.
*   **Solution:** We would need clear "Data Coverage" indicators in the UI to manage expectations.

## 3. Strategic Gains (Network Enrichment)

The primary reason to do this is the **Relational Geography** mentioned in the paper:
*   **Cross-County Clusters:** Many institutional investors (like *Amherst/BAF Assets*) cluster heavily in Gwinnett and Clayton. 
*   **SOS Bridge:** Because we use **Registered Agent IDs** and **SOS Principal Addresses** to link clusters, adding these counties would likely trigger a "Merge Wave." 
*   **Outcome:** We would see the *true* scale of these landlords. A cluster that currently looks like a "Mid-sized" 100-parcel group in Fulton might reveal itself as a 1,500-parcel "Mega-cluster" once the Gwinnett and Clayton holdings are linked via their shared SOS identity.

## 4. Implementation Effort

1.  **Ingestion:** Create `scripts/01c_load_gwinnett.py`, etc.
2.  **Normalization:** Run existing address normalization (`scripts/03`) on the new set.
3.  **Matching:** Rerun `scripts/08` (Parallel Matching).
4.  **Network:** Rerun `scripts/10` (Network Enrichment).

**Total Estimated Time:** 4-6 hours of automated processing + 2 hours of script adaptation.
