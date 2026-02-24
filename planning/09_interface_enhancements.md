# Plan: Interface Enhancements — Who Owns Atlanta?

**Created:** 2026-02-22
**Status:** In progress — sections 1–6 complete + geo leaderboard hierarchy complete (officers now sourced from sos.officers)

**Also done (tracked separately):**
- Data provenance — `datasources.json` + parcel panel source dividers + FAQ accordion → `planning/10_data_provenance.md` (commit 473484a, 2026-02-22)

---

## 1. Parcel panel — data to add ✅ DONE (ca4a354)

### Add (clearly useful)

| Field | Source | Notes | Status |
|---|---|---|---|
| County | already in API response | Display explicitly — determines which external links to construct | ✅ |
| Parcel ID | already in API response | Show it; investigators copy this to look up records elsewhere | ✅ |
| Owner mailing address | Fulton: `owneraddr1`/`owneraddr2`; DeKalb: `pstladdress`/`pstlcity`/`pstlstate`/`pstlzip5` | Reveals PO boxes, out-of-state addresses, shared addresses across shell companies | ✅ |
| Homestead exemption status | Fulton: `excode` | Translated: non-empty excode = "Homestead exempt", empty = "Not homestead exempt". Raw code not shown. | ✅ |
| Assessed/appraisal value | DeKalb: `totapr1` | Shown for DeKalb parcels with "(DeKalb)" label. Fulton not available. | ✅ |
| Zoning | DeKalb: `zoning` | Brief display. Skipped if blank (API returns NULL). | ✅ |
| Historic / overlay district | DeKalb: `histdesc`, `ovldesc` | Skipped if blank. | ✅ |
| Second owner name | DeKalb: `ownernme2` | Skipped if blank. | ✅ |
| Property class | Fulton: `classcode`; DeKalb: `classdscrp` | Translated via `GA_PROPERTY_CLASS` lookup (State of Georgia stratification codes — same codes in both counties). Sources: dekalbcountyga.gov/property-appraisal/appraisal-definitions + docs/FultonCountyPropertyClasses.pdf | ✅ |

### Skip (noise > signal)

- Raw geometry coordinates
- `shape__area` / `shape__length` in raw units (acreage already shown)
- Internal Fulton neighborhood code (`nbrhood`) — `city_neighborhood` is the human-readable version
- `featureid` — internal GIS artifact
- Subdivision details (`subdiv`, `subdivlot`, `subdivblck`, etc.) — narrow use case

---

## 2. External links from parcel panel ✅ DONE (ca4a354)

### High priority

**qPublic — primary "dig deeper" link.** ✅ Implemented.
- Fulton: `AppID=936`, DeKalb: `AppID=994`. KeyValue = parcel_id (URL-encoded, space-separated format confirmed working).
- Note: qPublic returns 403 to curl (bot protection) but links work fine in browser.

**GA SOS direct link** ✅ Implemented. `sos_business_id` fetched from `owner_entities` in the cluster sub-query and added to API response. Link shown only when non-null.

**Google Maps (property address)** ✅ Implemented. Labeled "Street View".

**Google Maps (owner mailing address)** ✅ Implemented. Labeled "Owner address map".

**OpenCorporates** ✅ Implemented. Shown only for `is_corporate` parcels. Links to `opencorporates.com/companies` search with `jurisdiction_code=us_ga`.

### Skip

- Generic web search by owner name — unreliable for common names; misleads more than helps. (Different logic applies on owner profile pages for corporate entities — see section 4.)
- Bizapedia — OpenCorporates covers the use case adequately.

---

## 3. Owner profile page — data to add


***Automated Owner "Persona" (The "So What?")***
We can pre-calculate a "Signature" for each owner on their profile page.:
 * The Hub Location: "This owner is based in Scottsdale, AZ (PO Box Pipeline)" or "Local Atlanta Operator."
   * County Focus: "Controls 15% of all corporate-owned parcels in [County Name]."
   * Portfolio Mix: (kind of exists) "Institutional-heavy (Trusts/Funds)" vs. "LLC-heavy (Developers)."


### Cluster-level data

| Field | Source | Notes |
|---|---|---|
| County breakdown | computed from parcel list | "237 parcels in Fulton, 98 in DeKalb" |
| SOS status | `ownership_clusters.primary_sos_status` | Display prominently with a colored indicator. Active = fine; Dissolved / Admin Dissolved / Owes Annual Registration = flag. A dissolved LLC collecting rent is a red flag. (as of 2/18/2026)|
| SOS registration date | `sos.entities.commencement_date` | When was this entity formed? |
| Foreign state of incorporation | `owner_entities.sos_foreign_state` | Wyoming / Delaware LLC owning 50 Atlanta properties vs. a Georgia LLC. |
| Business purpose | `sos.entities.business_type_desc` | Often blank; show when present. "Property management" vs. something incongruous. |
| Principal office address | `sos.addresses` | Residential address? PO box? Same address as another cluster? |
| Owner mailing addresses | `ownership_clusters.owner_addresses[]` | All mailing addresses across entities in cluster — reveals shared PO boxes, out-of-state management. |
| Neighborhood concentration | computed from parcel list | Top 3–5 neighborhoods by parcel count, with percentages. Clusters often dominate specific neighborhoods. |

### Potential Future additons with More Complete Data
- _complaints_ - need more County/City permits pulls
- _neighborhoods_ - need more County/City spatial data

| Field | Source | Notes |
|---|---|---|
| Permit density | computed: total permits ÷ parcel count | "2.3 complaints per property on average." Useful for comparing against leaderboard context. |


**Assessment value total** — rolling up DeKalb `totapr1` across all cluster parcels gives "this owner controls an estimated $X in property." Powerful. Caveat: Fulton values not available the same way. Options: show DeKalb-only total with label, show combined if Fulton value is available, or skip to avoid misleading partial totals.

**Exempt vs. non-exempt parcel count** — "0 of 50 Fulton parcels have homestead exemption" = clear "all rentals" signal. Fulton only. Worth showing even as a Fulton-specific figure.

---

## 4. Internal linkage

These connections within our data are the most valuable for investigation. All computable at static page build time.

### Registered agent → all clusters

If the RA is an individual (not a commercial RA factory), link to a pre-generated RA page listing all clusters managed by that person.

**Skip commercial RA firms** — Corporation Service Company, CT Corporation System, Registered Agents Inc, Northwest Registered Agent, Cogency Global, United States Corporation Agents, etc. These manage thousands of entities and the connection is not meaningful. Threshold suggestion: only link if the RA appears in ≤ N clusters (e.g. ≤ 25). Tune N after seeing the distribution.

### Shared officer → all clusters

Same logic as RA. An officer who appears as principal/member/manager across 5 different LLCs is a node in a network. Pre-generate per-person pages (or at minimum, show related clusters inline on the owner profile).

### Shared mailing address → all clusters

A PO box or street address shared by multiple distinct owner entities is a meaningful signal. Link to clusters sharing that address.

### Neighborhood / NPU / Council District → leaderboard filtered by area

From the parcel panel: "See top owners in Vine City" links to a pre-generated neighborhood leaderboard page. From the owner profile: "View all parcels in [neighborhood]" links to the filtered map view or a neighborhood stats page.

### Pre-generated pages to add

- **Per-individual-RA page** — `/agent/{slug}/` listing all clusters, total parcels, link to each owner profile
- **Per-neighborhood leaderboard** — `/neighborhood/{slug}/` top owners by parcel count within that boundary
- These are cheap to generate at build time and high value for investigators following a thread

---

## 5. "Related owners" section on owner profile

A simple list, not a diagram. Computed at static page build time.

**Format:**
> **Related owners**
> Connected via shared registered agent, officer, or mailing address.
>
> | Name | Connection | Parcels |
> |---|---|---|
> | SMART MANAGEMENT LLC | Shared RA: Charles Newlin | 12 → [link] |
> | WKK ATTRACTIVE LLC | Shared RA: Charles Newlin | 8 → [link] |
> | FLATSHOALS USA PROPERTIES LLC | Shared officer: ... | 6 → [link] |

***More on an Internal "Connection Ledger" (Transparency)***
  The user sees a cluster of 50 LLCs and asks "Why are these together?" We can replace the "Fluff" of a network
  diagram with a Evidence Table:
- Linkage Breakdown: A simple list: "Linked to [Company X] via Shared Officer [John Doe]" or "Linked to [Company Y] via Mailing Address [123 Main St]".
- Relationship Strength: Tag links as Strong (Shared Officer/Trusted Address) or Moderate (Shared Registered Agent/SOS Address). This helps the investigator know which links are "smoking guns."


**Rules:**
- Only show non-commercial-RA connections (same threshold as above)
- Cap displayed results at 10–15; add "and N more" if needed
- If no meaningful connections exist, omit the section entirely

**Network diagram — hold off.** A D3 force-directed graph looks impressive, is rarely used, and is hard to read when dense. The table above answers the same investigative question. Revisit only if the table proves insufficient for heavily networked clusters.

**Connection count on leaderboard** — a "(4 connected)" badge next to a cluster on the leaderboard adds triage signal at no cost. Investigators can identify interesting nodes before clicking in.

---

## 6. Leaderboard additions ✅ DONE

Currently shows: rank, owner names, parcel count, acreage, corporate/institutional flags.

***Additions per row:***
| Addition | Notes | Status |
|---|---|---|
| Connection count | Number of related clusters via shared RA/officer. Sortable. | ✅ Done |
| Link to /agents/ | Add "Registered Agents" link in the leaderboard page nav / header alongside the existing Leaderboard link. | ✅ Done |

## 6b. Geographic leaderboard hierarchy ✅ DONE

URL structure under `/l/`:
- `/l/` — global leaderboard (also backward-compat `/leaderboard/`)
- `/l/agents/` — registered agents index (also backward-compat `/agents/`)
- `/l/atlanta/neighborhood/{slug}/` — 245 pages, one per city neighborhood
- `/l/atlanta/council/{n}/` — 12 pages, one per council district
- `/l/atlanta/npu/{letter}/` — 25 pages, one per NPU
- `/l/fulton/` — Fulton County parcel owners
- `/l/dekalb/` — DeKalb County parcel owners

Each area page shows: rank, owner name, parcels in area, total parcels, corporate/institutional flags.
`city_neighborhood`, `city_council`, `city_npu` are City of Atlanta fields present on both `fulton_parcels` and `dekalb_parcels` tables — DeKalb entries are Atlanta parcels straddling the county line. No county-specific neighborhood pages needed.

Fast query pattern: `CROSS JOIN LATERAL unnest(oe.parcel_ids)` from `owner_entities` side + JOIN to area_map CTE (1.7s vs 2m22s for reverse direction).


***Potential Future additions per row:***

| Addition | Notes |
|---|---|
| SOS status indicator | Colored dot: Active = green, Dissolved = red, Owes AR = yellow. Requires more frequent updates (not yearly) |
| Permit rate | Complaints per property. Sortable column. Requires more County/City permit integration like above |

All of these are computable at static build time from existing data.

---

## 7. Implementation notes (for later)

- ~~Owner mailing address must be added to the `/api/parcel/` response~~ ✅ Done
- ~~Fulton `excode` needs a lookup table~~ ✅ Done — binary translation (non-empty = homestead exempt). Full per-code translation not needed; all values are homestead variants.
- ~~DeKalb `totapr1` is not currently returned by the parcel API~~ ✅ Done
- ~~qPublic URL formats need to be verified~~ ✅ Confirmed — bot protection blocks curl but URLs work in browser
- ~~GA SOS `sos_business_id` availability~~ ✅ Done — field is on `owner_entities`, fetched in cluster sub-query, shown when non-null
- ~~RA/officer relationship data needs a build-time query~~ ✅ Done — `fetch_linkable_agent_ids()` + `fetch_agent_clusters()` + commercial RA blocklist implemented; 24 individual agents linked
- ~~The commercial-RA threshold needs validation~~ ✅ Done — blocklist approach used (ILIKE patterns); `fetch_address_linkage()` adds shared mailing address connections (269 address groups, 2–10 clusters each)
- `GA_PROPERTY_CLASS` hardcoded in `app.js` — same State of Georgia codes used in both counties. DeKalb `classdscrp` column name is misleading; it stores codes not descriptions.

---

## 8. Things to explicitly not do

- Officer/RA names → generic web search links (common names will mislead)
- Geom coordinates or raw shape areas on any public page
- Assessment value totals for cross-county clusters where data is partial — present with caveat or skip
- Data pipeline timestamps / internal metadata on public pages
- Network diagram until/unless the table form proves insufficient
