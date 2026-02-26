# Planning: Cluster Refinement Strategy (Cluster 1 & 2)

## 0. Problem Statement
Large ownership clusters (Cluster 1 and Cluster 2) have been "merged" via professional service providers (Organizers, RAs, and Address Hubs) or builder-to-buyer artifacts. While the individual links are "weak," they pass current dataset-local frequency filters and create false ownership chains between unrelated entities (e.g., D R Horton and Invitation Homes).

## 1. Step 1: SOS Address Building-Level Normalization
**Target:** Address hubs like `1441 Woodmont Ln NW` (1,894 entities) and `103 Hickory Ave` (77 entities) that bridge unrelated entities because unit-level counts fall below the current threshold.

*   **Action:** Update `scripts/10_sos_network_enrichment.py`.
*   **Logic:** In `add_sos_addr_edges`, apply `normalize_street()` to the SOS principal address *before* calculating the frequency count.
*   **Outcome:** If a building (e.g., Woodmont) has >100 entities across all units, all unit-level bridges at that building will be disqualified.

## 2. Step 2: Global Officer Frequency & Role Filtering
**Target:** Cluster 1. Breaking bridges created by "Organizers" like `MORGAN NOBLE` who appear in thousands of SOS records but only a few dozen in our local parcel dataset.

*   **Action:** Update `scripts/10_sos_network_enrichment.py`.
*   **Logic:** 
    *   Modify `add_officer_edges` to perform a "global check" against the full `sos.officers` table.
    *   Filter out any officer where `(description IN ('Organizer', 'Incorporator')) AND (global_sos_count > 500)`.
*   **Outcome:** `MORGAN NOBLE` and `RILEY PARK` are reclassified as professional services and no longer act as bridges.

## 3. Step 3: Tighten Developer-Address Gating
**Target:** Cluster 2. Preventing the bridge at `100 CABOTS COVE CT` where D R Horton (Builder) is linked to individual buyers/investors.

*   **Action:** Update `scripts/04_ownership_network.py` and `scripts/10_sos_network_enrichment.py`.
*   **Change A:** Finalized `STREET_ENTITY_LIMIT` at **30** (lowered from 50).
*   **Change B:** Implement a "Builder-Buyer" heuristic. If an address contains a known corporate developer (e.g., `D R HORTON`, `BROCK BUILT`, `PULTE`) and also contains multiple individual/unflagged owners, skip address-based edges at that location.
*   **Outcome:** Builder offices and residential "buyer hubs" no longer bridge the developer to the buyers.

## 4. Issue: Adjustment of Merge Backstop (`MAX_MERGE_PARCELS`)
*   **Status:** **DEFERRED / NOT PERFORMED.**
*   **Reasoning:** Lowering the merge backstop (e.g., from 10,000 to 2,000) would likely "mask" the root causes identified above rather than fixing them. A lower backstop might also prematurely split valid large portfolios. The focus remains on improving the structural accuracy of the connection logic.

## 5. Implementation Results (Confirmed)
The strategy was implemented and verified on 2026-02-25:

*   **Fractured Mega-Clusters:** 
    *   `D R HORTON` (now Cluster 67) isolated to ~387 parcels.
    *   `Invitation Homes` (now Cluster 2240) isolated to 2 parcels.
    *   Previous Cluster 1 (3,860 parcels) split into coherent sub-groups (e.g., `FYR SFR`, `LARKIN STREET`).
*   **Tuned Limits:** `STREET_ENTITY_LIMIT` was finalized at **30**. This was low enough to block builder-to-buyer bridges (e.g., Cabots Cove) while high enough to maintain legitimate operator groups (e.g., `WEST MIDTOWN PARTNERS` / Sheth-Windham group at 403 W Ponce De Leon).
*   **Building-Level Normalization:** Verified that unit-stripped SOS addresses correctly identified hubs like Woodmont Ln.
*   **Global Officer Filtering:** `MORGAN NOBLE` and `RILEY PARK` were successfully reclassified as professional services and no longer act as bridges.
*   **Automated Recovery:** `build_static_pages.py` now automatically recreates `mv_leaderboard` and `mv_cluster_stats` if they are missing (via `ensure_materialized_views`).

## 6. Clusters to Re-Check (Post-Implementation Status)
1.  **Valid Institutional:** `SFR XII NM ATL OWNER 1 LP` - **STABLE** (~2,335 parcels).
2.  **Atlanta Operator:** `STRYANT HOMES` - **STABLE** (Cluster 18, 246 parcels).
3.  **Atlanta Operator (Hub-based):** `WEST MIDTOWN PARTNERS` - **STABLE** (Cluster 136, merged with Cluster 15872 as expected after tuning limit to 30).
4.  **Specific Development:** `HUNTCLIFF L L C` - **STABLE** (Cluster 93, 245 parcels).
5.  **Mega-Cluster 1 & 2:** **FIXED** (Successfully fractured).
