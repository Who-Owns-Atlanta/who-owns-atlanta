# Plan 24 — Individual Clustering Refinement (Anti-Megalord)

**Type B data release** → target `v202603B.1` (or next sequence)

---

## Background

The current clustering logic builds edges between entities based on **Name** or **Address**. This works well for corporate owners but creates false "Megalord" clusters for individuals with common names (e.g., "SMITH BARBARA J", "JOHNSON MICHAEL"). These clusters consist of completely unrelated individuals who happen to have the same name and use their own residential properties as mailing addresses. 

However, naively splitting all individuals by address breaks legitimate individual portfolios where the owner has minor typos in their mailing address or a mix of ZIP+4s (e.g., "SORROW MELVIN W").

This plan introduces a three-pronged approach to accurately split false individual clusters without damaging real portfolios.

---

## 1. Homestead Exemption Clash Rule

Under Georgia law, an individual can only claim a homestead exemption on their primary residence. If a single name is associated with multiple distinct addresses, and *multiple* properties have homestead exemptions, they are legally different people.

### Implementation
- **`scripts/utils.py`**:
  - In the Fulton `SELECT` for `parcels_unified`, add: `(excode IS NOT NULL AND excode != '') AS has_homestead`
  - In the DeKalb `SELECT`, add: `FALSE AS has_homestead` (DeKalb data does not currently provide homestead flags; this fallback safely allows DeKalb-Fulton edge evaluation without preventing valid cross-county matches).
- **`scripts/04_ownership_network.py` (`build_owner_entities`)**:
  - Aggregate `BOOL_OR(has_homestead) AS has_homestead` when generating the `tmp_raw_entities` and `owner_entities` tables.
- **`scripts/04_ownership_network.py` (`_get_edges` and `build_network`)**:
  - Pass the `has_homestead` flag alongside `eid` into the edge generation worker.
  - **Rule**: If an individual name has multiple distinct homesteads, **do not create Name Edges** for them.

---

## 2. Static Blocklist for "Junk" Names

Several non-informative placeholder names currently generate massive clusters because they bypass the `NAME_ENTROPY_LIMIT` (which was raised to 100 to accommodate corporate hubs). 

### Implementation
- **`scripts/04_ownership_network.py`**:
  - Introduce a `JUNK_NAME_BLOCKLIST` set (e.g., `{'RESTRICTED', 'UNKNOWN OWNER', 'CURRENT RESIDENT', 'ESTATE OF', 'UNKNOWN'}`).
  - When filtering names for the Name Edges step, automatically skip any name that exactly matches or starts with these blocklisted strings.
  - *Result*: These records will only cluster if they share an exact mailing address.

---

## 3. Dynamic Gating: Common vs. Rare Individual Names

Instead of blindly trusting name-links for all individuals, we will dynamically determine if a name is "Common" or "Rare" based on how many distinct addresses it appears at globally.

### Implementation
- **`scripts/04_ownership_network.py`**:
  - Introduce a new limit: `INDIVIDUAL_NAME_ENTROPY_LIMIT = 5`.
  - When building the name entropy dictionary, compute entropy for *all* names (including corporations/institutions).
  - During the Name Edges loop:
    - If the entity is `is_corporate` or `is_institutional`, continue using the existing high `NAME_ENTROPY_LIMIT` (100).
    - If the entity is strictly an individual, check its entropy against `INDIVIDUAL_NAME_ENTROPY_LIMIT` (5).
    - If an individual name appears at > 5 distinct addresses globally, it is flagged as a "Common Name" and is skipped for Name Edges entirely. (They will only cluster via Address Edges).
  - *Result*: "SMITH MICHAEL" (15+ addresses) is split into individual homeowners. "SORROW MELVIN" (4 addresses due to typos) is safely linked by name.

---

## Immediate Next Steps (Auto-Edit Mode)

Because the script modifications have been prototyped, the next steps are:
1. Save this plan document to `./planning/24_individual_clustering_refinement.md` in the project repo.
2. Ensure the materialized views are refreshed and all data dependencies in PostGIS reflect the new clustering.
3. Rebuild the static web pages (`scripts/build_static_pages.py`).
4. Rebuild the vector tiles (`scripts/build_tiles.sh`).
5. Ensure everything is staged/committed as appropriate.