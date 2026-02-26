# 13. Institutional Clustering Refinement Plan

## Objective
Restore the integrity of major institutional landlord clusters (Invitation Homes, Progress Residential, American Homes 4 Rent) which were fragmented during recent "mega-cluster" prevention measures. We aim to achieve "high-signal unification" without re-introducing the massive, unrelated clusters (Cluster 1 / Cluster 2 noise).

## Status: Partially Complete (script 10b implemented)
- **Fragmentation Detected:** Progress Residential (748 parcels vs ~2,000 expected) and Invitation Homes (split across 11+ clusters).
- **Root Cause:** PO Box stripping in normalization and overly strict street-level entropy gating (30 entities) severing corporate headquarters.

## Proposed Changes

### 1. Preserve PO Boxes in Normalization
- **File:** `scripts/03_normalize_addresses.py`
- **Change:** Add `po_box` to the list of components preserved by the `parse_address` function.
- **Expected Outcome:** Progress Residential and AMH entities will match on their specific PO Boxes rather than collapsing to generic "City, State, Zip" strings.

### 2. Tiered Street Entropy Gating & Mis-Bridge Fix
- **Problem:** Entities like "HOME SFR" (Pretium) and "BAF/ALTO" (Amherst) are bridged by secondary addresses that vary slightly (e.g., "8300 N MOPAC") and fall below the 30-entity limit.
- **Change:** 
    - **Global Entropy Reduction:** Lower the base `STREET_ENTITY_LIMIT` to **10-15** for corporate/institutional entities if they are being bridged to *different* name stems.
    - **Address Canonicalization:** Add a step to "collapse" known office park variations (e.g., mapping all "5001 PLAZA ON THE LAKE" variants to a single hub) before counting entities.
    - **Corporate Hub Blocklist:** Maintain a small, high-confidence list of addresses that are known to be "Professional Hubs" (like the Scottsdale PO Box and the Austin office parks) that should *always* be gated, even for corporate entities.
- **Expected Outcome:** Cluster 3 will split into separate Pretium and Amherst clusters.

### 3. Corporate Series Name Bridging
- **File:** `scripts/04_ownership_network.py`
- **Change:** Add a name-stemming pass for corporate series (e.g., "BORROWER 1" vs "BORROWER 2").
- **Constraint:** Bridge if:
    1. Both entities share a significant name stem (e.g., "PROGRESS RESIDENTIAL BORROWER").
    2. Both are `is_corporate`.
    3. Both share a mailing `City` and `State`.
- **Expected Outcome:** Invitation Homes and Progress series will bridge internally, reducing the reliance on "weak" address links.

### 4. Validation Baseline
- **Verification Script:** Use SQL or a custom script to verify against *Horizontal Holdings* benchmarks for Fulton + DeKalb:
    - **Invitation Homes:** > 2,500 parcels.
    - **Progress Residential:** > 2,000 parcels.
    - **Mega-Cluster Check:** Largest cluster remains < 5,000 parcels.

## Implemented: script 10b_cluster_refinement.py (2026-02-26)

Rather than modifying scripts 03/04/10, a new post-processing script was added:
`scripts/10b_cluster_refinement.py` — runs after script 10, before materialized view rebuild.

**Pass A (Name-Series Fusion):** ✅ Working
- 723 groups merged, 1,333 entity reassignments
- Invitation Homes (IH BORROWER, SFR XII, TBR SFR, STAR BORROWER series): all unified → cluster 8, **3,315 parcels**
- Progress Residential (BORROWER 1–25 series): unified → cluster 77, **718 parcels**
- Amherst Holdings (BAF ASSETS, ALTO ASSET, SRMZ, etc.): unified → cluster 3, **3,172 parcels**

**Pass B (Fission):** ⚠️ Partial
- Cluster 77: MILE HIGH BORROWER (52 parcels, Denver-based) correctly split off → new cluster 468331
- Cluster 3 (Pretium/Amherst over-merge): **NOT split** — see Known Limitation below

**Known Limitation — Cluster 3 (Amherst + Pretium false merge):**
- Pretium's FYR SFR BORROWER and Amherst's HOME SFR BORROWER both use 3505 Koger Blvd
  Suite 400, Duluth GA 30096 as their Georgia mailing address (same physical office)
- This creates a genuine address edge in G_sub that can't be severed algorithmically
- Additionally, FYR SFR BORROWER and HOME SFR BORROWER share "SFR" + "BORROWER" tokens
  (50% Jaccard similarity), so name-based meta-graph analysis can't separate them
- Fixing this would require either: (a) hardcoded firm knowledge, or (b) the
  "Corporate Hub Blocklist" approach from the original plan (blocking Koger Blvd as a hub)

## Execution Steps
1. [x] Implement `scripts/10b_cluster_refinement.py` (Pass A + Pass B)
2. [ ] Optional: Update `scripts/04_ownership_network.py` to add Koger Blvd to BUILDER_KEYWORDS
       or street hub list — this would prevent the Pretium/Amherst merge at source
3. [ ] Optional: Lower base `STREET_ENTITY_LIMIT` for known corporate hub addresses
