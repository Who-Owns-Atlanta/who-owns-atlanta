# Plan: Data Provenance — Global datasources.json + Footnote Pattern

**Created:** 2026-02-22
**Status:** ✅ DONE (commit 473484a, 2026-02-22)

---

## Goal

Single source-of-truth attribution file drives all data sourcing across the site.
Individual pages show a lightweight `*` footnote resolving to a full data sources
listing on the FAQ. No build step. No inline dates. Scales to all future pages.

---

## What was built

### `web/frontend/data/datasources.json` ✅

Hand-maintained JSON served at `/data/datasources.json` by nginx (falls through
`location /` → `/var/www/who-owns-atlanta/frontend`). 7 entries:

| Key | Name | Admin date |
|---|---|---|
| `fulton_parcels` | Fulton County Tax Assessor | 2026-01-07 |
| `dekalb_parcels` | DeKalb County Tax Assessor | 2025-12-06 |
| `atlanta_gis_neighborhoods` | City of Atlanta GIS — Neighborhoods | 2025-02-05 |
| `atlanta_gis_npu` | City of Atlanta GIS — NPU | 2025-02-05 |
| `atlanta_gis_council` | City of Atlanta GIS — Council Districts | 2026-02-09 |
| `accela` | City of Atlanta — Accela | — |
| `ga_sos` | GA Secretary of State, Corporations Division | 2026-02-18 |

Dates sourced from `metadata.json` (ArcGIS `modified` timestamps) and known load dates.
Atlanta GIS layers split into 3 entries because they have different admin dates.
Edit this file on each data refresh — no code change needed.

### Parcel panel restructure ✅

Two `<dl>` elements now, with mailing address block between them:

```
COUNTY TAX PARCEL *
  County / Parcel ID / Property class / Co-owner
  Land / Units / Land use / Exemption / Assessed value
  Zoning / Historic / Overlay

OWNER MAILING ADDRESS  (standalone block, as before)
  608 PARK AVE SE
  ATLANTA GA 30312

CITY OF ATLANTA GIS *
  Neighborhood / NPU / Council
```

- `*` is a superscript `<a>` linking to `/faq/#data-sources`
- County divider has no top border/gap (`:first-child` rule)
- City dl is hidden when parcel has no city fields
- `ⓘ Data sources` link at bottom of `#parcel-view` → `/faq/#data-sources`

### FAQ data-sources accordion ✅

`<details id="data-sources">` at bottom of FAQ. Lazy-fetches `/data/datasources.json`
on first open, renders an attribution table (Source | What it provides | Admin date | Last loaded | Link).

---

## Files changed

| File | Change |
|---|---|
| `web/frontend/data/datasources.json` | Created |
| `web/frontend/index.html` | Added `#owner-mail-addr` + `#parcel-meta-city` + `.sources-footnote` |
| `web/frontend/js/app.js` | Split meta into two dls; `renderDivider()` helper; `parcelMetaCity` global |
| `web/frontend/css/style.css` | `.meta-source-divider`, `.meta-source-divider:first-child`, `.sources-footnote` |
| `web/frontend/faq/index.html` | Added `<details id="data-sources">` with inline fetch+render script |

---

## Convention established (project-wide)

Every page that displays data from a non-obvious source:
1. Uses a `<dt class="meta-source-divider">` or equivalent section label with `*` linking to `/faq/#data-sources`
2. Has `<a class="sources-footnote" href="/faq/#data-sources">ⓘ Data sources</a>` at the bottom

Future pages (owner profile, agent page, leaderboard) each need one `.sources-footnote`
line and whatever group dividers are relevant. The FAQ table is already there.
