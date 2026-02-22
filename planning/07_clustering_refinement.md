# Clustering Refinement Methodology

## Problem Statement
The current ownership clustering logic (Scripts 04 and 10) successfully groups related LLCs but suffers from "Mega-Cluster Expansion." This occurs when unrelated entities are erroneously linked through professional service providers, shared commercial "hubs," or poor address normalization.

### Key Observations
1.  **Professional Service Hubs (RA/Address):** Registered Agent firms (e.g., *Homeowner Management Services Inc.*) and mailbox centers (e.g., `2472 JETT FERRY RD`) act as "glue" for hundreds of unrelated entities.
2.  **Address Variant "Leakage":** The `BASE_MAX_ADDR_ENTITIES` limit of 10 is bypassed when a single building uses multiple suite variants (e.g., `STE 400` vs `STE 400-321`), each having fewer than 10 entities.
3.  **Generic Name Collisions:** Names like "BRANDYWINE" or "TANGLEWOOD" are often housing development labels rather than owner names, linking hundreds of individual homeowners into a single cluster.
4.  **SOS Daisy-Chaining:** In the SOS enrichment pass (Script 10), multiple small clusters can be merged into one massive cluster in a single iteration if they share an Officer or RA, even if the resulting cluster exceeds the "small portfolio" intent.

## Proposed Refinement Strategy

### 1. Street-Level Address Gating (Entropy Rule)
To prevent mailbox centers and office parks from acting as bridges:
*   **Rule:** When calculating the `BASE_MAX_ADDR_ENTITIES` limit (currently 10), the system will normalize addresses to the **Street Level** (stripping all Unit/Suite/Apt info).
*   **Impact:** If `2472 JETT FERRY RD` has 72 entities across all suites, it will be flagged as a "Hub" and skipped for `same_addr` edges, even if each individual suite has only 1 entity.

### 2. Name Entropy Filter
To prevent "Name-Only" merges for generic labels:
*   **Rule:** Calculate the "Address Entropy" for every owner name. If a name (e.g., "BRANDYWINE") is associated with more than **5 distinct normalized addresses**, it is tagged as "Generic."
*   **Impact:** Name-based edges will be skipped for "Generic" names. Legitimate developers usually use 1-2 consistent mailing addresses for all their LLCs.

### 3. Expanded Professional Blacklist
Update the `COMMERCIAL_RA_SKIP` list in `scripts/10_sos_network_enrichment.py` to include:
*   **HOA Managers:** *Homeowner Management Services Inc.*, *Community Management Associates*, *Fieldstone Realty Partners*.
*   **Commercial Filing Services:** *Registered Agents Inc*, *Georgia Registered Agent LLC*, *BCS Corporate Services*.

### 4. SOS Merge "Size Gate" (Cluster Integrity)
To prevent "Daisy-Chaining" in Pass 2:
*   **Rule:** An SOS-derived edge (RA, Officer, SOS Address) will only be added if the *resulting merged cluster* would not exceed a specific threshold (e.g., 500 parcels), UNLESS the entities already share a "Trusted" link (like an exact Name + Address match).
*   **Impact:** Prevents a single RA from linking five 100-parcel clusters into one 500-parcel cluster in one pass.

## Success Criteria
*   **Cluster 1 (9,496 parcels):** Should break into ~400+ independent communities (HOAs, small developers).
*   **Cluster 3 (990 parcels):** Should dissolve into hundreds of single-parcel entities.
*   **Legitimate Aggregators:** Large SFR portfolios (e.g., *Invitation Homes*) must remain intact, as they typically share a unique "Private" address or a consistent name pattern.

## Implementation Plan
1.  **Data Analysis:** Verify the "Name Entropy" thresholds across the DB.
2.  **Script Update (04):** Implement Street-Level Address Gating and Name Entropy in the base network script.
3.  **Script Update (10):** Update RA Blacklist and implement the SOS Size Gate.
4.  **Validation:** Run comparison stats (Cluster Count, Mega-Cluster Size) before committing changes.
