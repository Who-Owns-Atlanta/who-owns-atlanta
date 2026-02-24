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

### 2026-02-23: "Horizontal Holdings" Alignment & Institutional Isolation

#### Issue
The "mega-cluster" (Cluster 1) remained bloated at 20k+ parcels, merging disparate corporate landlords (Invitation Homes, D.R. Horton, etc.) through shared institutional "hubs" like Development Authorities and professional Registered Agent addresses that were not sufficiently filtered by street-level entropy.

#### Actions
1.  **Institutional Isolation (`scripts/04_ownership_network.py` & `scripts/10_sos_network_enrichment.py`)**:
    *   Explicitly excluded all entities flagged as `is_institutional` from forming edges in the ownership graph.
    *   This prevents "Development Authorities" or "Housing Authorities" from acting as bridges between unrelated corporate property owners.
2.  **Expanded RA Skip List (`scripts/10_sos_network_enrichment.py`)**:
    *   Added more professional proxy agents to the `COMMERCIAL_RA_SKIP` list: `ZenBusiness Inc`, `Registered Agent Solutions Inc`, `CSC of Cobb County, Inc.`, and `Northwest Registered Agent Service, Inc.`.
3.  **Benchmarking against "Horizontal Holdings"**:
    *   Validated results against the methodology in Shelton & Seymour (2024).
    *   Adjusted for 2-county coverage (Fulton/DeKalb), our consolidated clusters now closely match the paper's 5-county findings.

#### Results
*   **Mega-Cluster Fragmented**: Cluster 1 (20k+) was broken into logical firm-level or sector-level clusters.
*   **Accurate Firm Totals**:
    *   **Invitation Homes (Cluster 2)**: ~5,072 parcels (matches expectation for 2 core counties vs 7.8k in 5 counties).
    *   **Amherst / Pretium (Cluster 4)**: ~3,599 parcels.
    *   **FirstKey Homes (Cluster 8)**: ~1,709 parcels.
*   **Developer Isolation**: Major homebuilders like D.R. Horton are now correctly isolated into their own clusters (e.g., Cluster 1 now represents a builder/developer hub).

### 2026-02-24: Advanced Institutional Isolation & SOS Gate Refinement

#### Issue
A new "mega-cluster" (Cluster 1) appeared at ~10k parcels, bridging major developers with HOAs and Condominium associations. This was caused by:
1.  **Incomplete Pattern Matching**: "Community Association" and "Condo" were not in the institutional blacklist.
2.  **SOS Bridging**: The SOS enrichment passes (RA/Officer) did not honor the `is_institutional` flag, allowing associations to bridge through professional agents.
3.  **Truncated Names**: DeKalb's name limits truncated "Association" to "Associatio", missing regex anchors.

#### Actions
1.  **Expanded Institutional Logic (`scripts/02_flag_corporate_owners.py`)**:
    *   Added `community associat`, `condo`, `condominium`, `townhouse`, `wildwood park`, and `oxford village` to the pattern.
    *   Implemented land-use/property-class based flagging: Fulton `lucode` (111, 166, 188, 208) and DeKalb `classcd` (R9), `landuse` (COS), and `common_area`.
2.  **SOS Gate Reinforcement (`scripts/10_sos_network_enrichment.py`)**:
    *   Explicitly excluded `is_institutional` entities from all SOS enrichment passes (RA, Officer, Address).
    *   Added `BILL WETTER` and `SENTRY MANAGEMENT` to the `COMMERCIAL_RA_SKIP` list.
3.  **Regex Softening**: Removed word boundary anchors (`\m`, `\M`) from the institutional pattern to capture truncated names from county exports.

#### Results
*   **Mega-Cluster Fragmented**: Cluster 1 (formerly 10k+) broken into logical firm-level clusters.
*   **Top 10 Accuracy**:
    *   **Invitation Homes**: ~5,020 parcels (Clean corporate cluster).
    *   **Amherst / Pretium (Progress)**: ~3,599 parcels.
    *   **FirstKey Homes**: ~1,709 parcels.
    *   **Developer Hubs**: D.R. Horton (~1,386 parcels) isolated from HOAs.
*   **Zero Institutional Noise**: Top corporate clusters now show `institutional_parcel_count = 0` in `mv_leaderboard`.

### Verification
Ran `scripts/investigate_cluster_1.py` (temporary) to confirm that the bridge path `HIGHLAND GREEN (HOA) -> GOULDING (HOA) -> FALLS AT CAMP CREEK -> D R HORTON` is no longer formed.

### Verification
Ran `scripts/investigate_cluster.py` to confirm that shortest paths between unrelated firms (e.g., BAF Assets to FYR SFR) are no longer present in the graph.
