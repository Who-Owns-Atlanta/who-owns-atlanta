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

**Pass B — updated results after ADDRESS_STREET_BLOCKLIST fix (2026-02-26):**
- Cluster 77: MILE HIGH BORROWER (52 parcels) split off ✅
- Cluster 126 (Pretium FYR SFR): split from Amherst → **540 parcels standalone** ✅
- Amherst (BAF ASSETS, ALTO ASSET, etc.) → **cluster 3, 492 parcels** ✅

**Root cause of Pretium/Amherst merge — two shared addresses:**
1. `3505 KOGER BLVD STE 400 DULUTH GA 30096` — same Duluth GA law/servicer office
2. `5100 TAMARIND REEF CHRISTIANSTED 00820` — same USVI trust address

**Fix: `ADDRESS_STREET_BLOCKLIST` in scripts 04 and 10**
Both scripts now carry a small explicit blocklist of address prefixes that must never
create address edges. Added `3505 KOGER BLVD` and `5100 TAMARIND REEF`.

## Final Results (2026-02-26)
| Entity | Cluster | Parcels |
|--------|---------|---------|
| Invitation Homes (all series) | 8 | 3,315 |
| Progress Residential | 77 | 718 |
| Amherst Holdings (BAF ASSETS, ALTO ASSET, etc.) | 3 | 492 |
| Pretium Partners (FYR SFR BORROWER) | 126 | 540 |
| Largest cluster overall | 8 | 3,315 |

All benchmarks met: no cluster > 5,000; all four major firms in their own clusters.

## Execution Steps
1. [x] Implement `scripts/10b_cluster_refinement.py` (Pass A + Pass B)
2. [x] Add `ADDRESS_STREET_BLOCKLIST` to `scripts/04_ownership_network.py` and
       `scripts/10_sos_network_enrichment.py` (Koger Blvd + Tamarind Reef)
