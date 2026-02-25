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
*   **Change A:** Lower `STREET_ENTITY_LIMIT` from 50 to 20. (`100 CABOTS COVE CT` has 22 entities).
*   **Change B:** Implement a "Builder-Buyer" heuristic. If an address contains a known corporate developer (e.g., `D R HORTON`, `BROCK BUILT`, `PULTE`) and also contains multiple individual/unflagged owners, skip address-based edges at that location.
*   **Outcome:** Builder offices and residential "buyer hubs" no longer bridge the developer to the buyers.

## 4. Issue: Adjustment of Merge Backstop (`MAX_MERGE_PARCELS`)
*   **Status:** **DEFERRED / NOT PERFORMED.**
*   **Reasoning:** Lowering the merge backstop (e.g., from 10,000 to 2,000) would likely "mask" the root causes identified above rather than fixing them. A lower backstop might also prematurely split valid large portfolios. The focus remains on improving the structural accuracy of the connection logic.

## 5. Validation Plan
1.  **Reproduction:** Run `scripts/investigate_cluster.py 1` and `2` before changes (already performed during research).
2.  **Execution:** Apply script updates and re-run the clustering pipeline:
    *   `uv run scripts/04_ownership_network.py`
    *   `uv run scripts/10_sos_network_enrichment.py`
    *   `PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl -f scripts/sql/04_create_materialized_views.sql`
3.  **Verification:** 
    *   Verify Cluster 1 and 2 IDs have changed/shrunk.
    *   Confirm `D R HORTON` and `IH2 HOLDINGS` are in separate clusters.
## 6. Clusters to Re-Check
The following clusters (current IDs/names) will be monitored to ensure the changes improve accuracy without breaking valid portfolios:
1.  **Valid Institutional:** `SFR XII NM ATL OWNER 1 LP` (Current ID 3, 5020 parcels) - Should stay together.
2.  **Atlanta Operator:** `STRYANT HOMES` (Current ID 4, 677 parcels) - Verify if it stays together or fractures legitimately.
3.  **Specific Development:** `HUNTCLIFF L L C` (Current ID 116, 248 parcels) - Monitor for changes.
4.  **Concentrated Institutional:** `PROMISE HOMES BORROWER I LLC` (Current ID 2240, 285 parcels) - Should stay together.
5.  **Mega-Cluster 1 & 2:** (Current IDs 1 and 2) - Should significantly fracture into their constituent parts.
