# Plan: Owner Profile Page — Section 3 Enhancements

**Created:** 2026-02-22
**Status:** Complete — 2026-02-22 (checkboxes updated 2026-02-28)
**Ref:** `planning/09_interface_enhancements.md` §3

---

## Context

The current owner profile (`scripts/build_static_pages.py` → `OWNER_TMPL`) is minimal:
owner name + badges, a stats row, a collapsible SOS block (status + state only), "View on map",
and a parcel table. This plan adds county breakdown, rich SOS detail, neighborhood concentration,
owner mailing addresses, and data provenance dividers consistent with the parcel panel pattern.

`ga_business` and `ga_business_officer` tables have been removed. All SOS detail
comes from the `sos` schema (sos.entities, sos.officers) joined via `sos_control_number`
on `owner_entities`.

**Sections 4 & 5 compatibility:** RA names shown as plain text only (section 4 adds links).
No related-owners section (section 5). No connection badges (section 6).

---

## 0. Dev workflow — `--cluster-ids` flag ✅

Add `--cluster-ids 1954,120,30,2` CLI arg to `build_static_pages.py`. When provided,
bypasses `fetch_cluster_ids()` DB scan and uses the supplied list directly.
Costs ~10 lines; saves rebuilding 40K pages during development.

**Test set:**
| Cluster ID | Parcels | Character |
|---|---|---|
| 1954 | 5 | Individual owners (AGARWAL family), no SOS, no city parcels |
| 120 | 50 | Corporate mix, Delaware LLCs, Sanctuary Park, ~155 acres |
| 30 | 335 | Large corporate cluster |
| 2 | 7,693 | Massive institutional (City of Atlanta, MARTA, Fulton County) |

```bash
# Dev rebuild — 4 pages only
uv run scripts/build_static_pages.py --owner-only --cluster-ids 1954,120,30,2
```

---

## 1. New DB queries — all batch, keyed by cluster_id ✅

Add three new functions to `build_static_pages.py` alongside existing batch queries.
Also augment the existing `fetch_cluster_stats_batch()` to pull `owner_addresses`.

### 1a. County breakdown — `fetch_county_breakdown_batch(conn, cluster_ids)`
```sql
SELECT cluster_id, county, SUM(count) AS parcel_count
FROM owner_entities
WHERE cluster_id = ANY(%s)
GROUP BY cluster_id, county
```
Returns: `{cluster_id: {'fulton': N, 'dekalb': N}}`

### 1b. SOS entity details — `fetch_sos_details_batch(conn, cluster_ids)`
```sql
SELECT cluster_id,
       sos_status, sos_foreign_state, sos_business_type,
       sos_registered_agent, sos_registered_agent_address,
       COUNT(*) AS entity_count
FROM owner_entities
WHERE cluster_id = ANY(%s) AND sos_status IS NOT NULL
GROUP BY cluster_id, sos_status, sos_foreign_state, sos_business_type,
         sos_registered_agent, sos_registered_agent_address
ORDER BY cluster_id, entity_count DESC
```
In Python, aggregate per cluster into:
- `statuses` — unique values sorted by count, e.g. `[('Active/Compliance', 18), ('Dissolved', 2)]`
- `states` — unique foreign_state values
- `business_types` — unique types sorted by count
- `agents` — unique `(name, address)` pairs, capped at 10

### 1c. Neighborhood concentration — `fetch_neighborhood_concentration_batch(conn, cluster_ids)`
```sql
SELECT oe.cluster_id,
       COALESCE(fp.city_neighborhood, dp.city_neighborhood) AS neighborhood,
       COUNT(*) AS parcel_count
FROM owner_entities oe
JOIN LATERAL unnest(oe.parcel_ids) AS pid ON true
LEFT JOIN fulton_parcels fp ON fp.parcelid = pid AND oe.county = 'fulton'
LEFT JOIN dekalb_parcels dp ON dp.parcelid = pid AND oe.county = 'dekalb'
WHERE oe.cluster_id = ANY(%s)
  AND COALESCE(fp.city_neighborhood, dp.city_neighborhood) IS NOT NULL
GROUP BY oe.cluster_id, COALESCE(fp.city_neighborhood, dp.city_neighborhood)
ORDER BY oe.cluster_id, parcel_count DESC
```
Take top 5 per cluster. Returns: `{cluster_id: [('Kirkwood', 45), ('Grant Park', 22), ...]}`

### 1d. Owner addresses — add to `fetch_cluster_stats_batch()`
Add `oc.owner_addresses` column to the existing stats query
(`ownership_clusters` already stores this array). Cap to 8 for display.

---

## 2. Updated `OWNER_TMPL` structure ✅

```
[header: name, alt-names, badges]  ← unchanged

[stats row]  ← keep existing 4 stats (parcels/acres/corporate/complaints)

COUNTY TAX PARCEL *                  ← p.profile-section-label with src * link
  [dl.profile-dl]
  Fulton County    X parcels         ← conditional, each county on own row
  DeKalb County    X parcels         ← conditional
  Acreage          X.X acres
  Complaints       X total, Y open   ← conditional if > 0
  [owner-addresses block]            ← if present; plain list capped at 8

GEORGIA SOS *                        ← details.sos-details, collapsible
  (omit entire block if no SOS data)
  [dl]
  Status           Active/Compliance  ← .sos-status-warn if dissolved/owes AR
  Formed in        Delaware           ← list if multiple states
  Type             Domestic LLC       ← list if multiple types
  Reg. agent       NAME               ← plain text wrapped in span.ra-name
                   ADDRESS            ← (section 4 adds /agent/ href here)

NEIGHBORHOOD BREAKDOWN *             ← section, only if any city_neighborhood present
  [simple table or dl: neighborhood → count, top 5]
  * → /faq/#data-sources

[View on map →]  ← unchanged

Parcels (N)
  [if N > 200]: note "Showing 200 of N — use map for full list"
  [table: Address | County | Owner on record | Flags]  ← cap at 200 rows

[ⓘ Data sources]  ← .sources-footnote → /faq/#data-sources
```

**Data provenance tagging convention** (consistent with parcel panel):
Section labels use `<p class="profile-section-label">LABEL<sup><a class="src-ref" href="/faq/#data-sources">*</a></sup></p>`.
SOS `<details>` summary gets the `*` inline. Neighborhood section header same.

**SOS status flagging:**
Statuses in `{'Dissolved', 'Admin. Dissolved', 'Owes Annual Registration'}` → `<span class="sos-status-warn">`.

---

## 3. CSS additions — `web/frontend/css/content.css` ✅

```css
/* Profile section label (County Tax Parcel *, Georgia SOS *, etc.) */
.profile-section-label { ... }   /* like meta-section-label in style.css */

/* Profile dl (2-col grid, same as parcel panel) */
.profile-dl { ... }

/* SOS flagged status */
.sos-status-warn { color: #dc2626; font-weight: 600; }

/* Neighborhood list */
.neighborhood-list { ... }

/* Source reference superscript * */
.src-ref { color: var(--pico-muted-color); font-size: 0.7em; text-decoration: none; }
.src-ref:hover { color: var(--pico-primary); }

/* Owner addresses list */
.owner-addr-list { ... }
```

---

## 4. `render_owner()` updates ✅

Pass all new data to the template:
- `county_breakdown` — dict `{fulton: N, dekalb: N}`
- `owner_addresses` — list, capped at 8
- `sos_details` — dict with `statuses`, `states`, `business_types`, `agents`
- `neighborhoods` — list of `(name, count)` top 5
- `parcels` — slice to first 200; pass `total_parcel_count` for the cap note

---

## 5. `worker()` / `build_owner_pages()` updates ✅

Incorporate the 3 new batch queries into the worker loop (alongside existing
`fetch_cluster_stats_batch` and `fetch_parcels_batch`). Pattern is identical —
query by `cluster_id = ANY(batch)`, key result by cluster_id.

---

## Files to change

| File | Change |
|---|---|
| `scripts/build_static_pages.py` | `--cluster-ids` flag; 3 new batch query functions; augment stats batch to include owner_addresses; updated `OWNER_TMPL`; updated `render_owner()`; 200-row parcel cap |
| `web/frontend/css/content.css` | New classes: profile-section-label, profile-dl, sos-status-warn, neighborhood-list, src-ref, owner-addr-list |

**Not touched:** `web/api/main.py`, `web/frontend/data/datasources.json`, map SPA.

---

## Sections 4/5 stubs left intentionally

| Feature | Status | Hook left |
|---|---|---|
| RA page links | Section 4 | `<span class="ra-name">` wraps agent name text |
| Related owners | Section 5 | Nothing — purely additive new section |
| Connection count | Section 6 | Nothing — purely additive stat |
| Officers table | When sos.officers has data | **Not implemented** — `sos.officers` has data (e.g. 227 rows for cluster 120, 259 for cluster 30) but `build_static_pages.py` has no query or template block for officers. The API (`main.py`) does fetch officers. This is a real gap to fill. |

---

## Verification

```bash
# Build test pages
uv run scripts/build_static_pages.py --owner-only --cluster-ids 1954,120,30,2

# Sanity check files exist
ls /var/www/who-owns-atlanta/owner/{1954,120,30,2}/index.html

# shot-scraper each
shot-scraper http://who-owns-atlanta.local/owner/1954/ -o /tmp/owner_1954.png
shot-scraper http://who-owns-atlanta.local/owner/120/  -o /tmp/owner_120.png
shot-scraper http://who-owns-atlanta.local/owner/30/   -o /tmp/owner_30.png
shot-scraper http://who-owns-atlanta.local/owner/2/    -o /tmp/owner_2.png
```

**Check per page:**
- 1954: no SOS block, no neighborhood block, county tax section shows, small table
- 120: SOS block (Delaware, Active), neighborhood block, ~50-row table, * links work
- 30: SOS with multiple agents/statuses, neighborhood list, table
- 2: institutional, table cap note "Showing 200 of 7,693", neighborhood list, ⓘ link

---

## Implementation order

1. `--cluster-ids` flag (5 min, unlocks fast iteration)
2. New batch query functions + augment stats fetch
3. `render_owner()` + `OWNER_TMPL` update
4. CSS additions
5. Visual check via shot-scraper
6. Full build + commit
