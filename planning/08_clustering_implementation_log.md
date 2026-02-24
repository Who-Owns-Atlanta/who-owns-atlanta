# Clustering Implementation Log

## 2026-02-22: Residential Focus & Noise Reduction Refinement

### Issue
The dataset was "polluted" with non-residential (industrial, public, utility) properties. Institutional entities like MARTA, Georgia Power, and Development Authorities were being mis-flagged as corporate, causing "mega-cluster" bloat (e.g., 7k+ parcels) and obscuring legitimate corporate residential landlords.

### Actions
1.  **Refined Flagging Logic (`scripts/02_flag_corporate_owners.py`)**:
    *   Moved public authorities (Development Authority, Housing Authority, etc.) and utilities (GA Power, MARTA, Railways) to the `is_institutional` flag.
    *   Ensured institutional flagging happens *before* corporate flagging to prevent Authorities with "Development" or "Real Estate" in their names from being treated as business entities.
2.  **Residential Filtering (`scripts/01_load_parcels.py`)**:
    *   Updated `parcels_unified` view to filter for residential classes (`R*`, `T*`) or Commercial classes with `living_units > 0`.
    *   Reduced total dataset from 615k to 576k parcels, purging industrial/public land noise.
3.  **Tuning Parameter Optimization (`scripts/04_ownership_network.py` & `scripts/10_sos_network_enrichment.py`)**:
    *   Increased `NAME_ENTROPY_LIMIT` from 10 to 100.
    *   Increased `MAX_MERGE_PARCELS` from 400 to 10,000.
    *   Kept `STREET_ENTITY_LIMIT` at 50 to maintain street-level gating for office park/condo "hairballs."

### Results
*   **Leaderboard Purged**: Institutional entities removed from top rankings.
*   **Consolidated Portfolios**: Legitimate residential mega-portfolios successfully unified:
    *   **Invitation Homes**: Surged to 4,288 parcels (unifying multiple SFR XII, STAR, TAH entities).
    *   **Amherst / Progress Residential**: Unified to 3,599 parcels.
    *   **FirstKey Homes**: Unified to 1,710 parcels.
*   **Mega-Cluster Reduction**: Large public "hubs" fragmented, focusing the network purely on private residential ownership.
*   **Increased SOS Linkage**: 53k RA edges, 20k Officer edges, and 40k SOS Address edges now active in the graph.

### Verification
Confirmed top 500 leaderboard (`mv_leaderboard`) now correctly reflects residential corporate landlords and developers without public/industrial noise.
