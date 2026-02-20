# Who Owns Atlanta? — Design Language (Project-Adapted)

> Adapted from `docs/design_language.md`. Stack and visual principles are preserved;
> everything specific to layout, components, and pages is tailored to this project.

***

## Core stack

- **Pico.css** — minimalist, semantic-first CSS; no class soup, plain HTML auto-styled. MIT licensed. [picocss.com](https://picocss.com)
- **MapLibre GL JS** for the interactive map layer.
- **OpenFreeMap** for the base tileset (fully open, OSM-backed). [github.com/hyperknot/openfreemap](https://github.com/hyperknot/openfreemap)
- **Vanilla JS only** — no frontend framework. The map page is JS-heavy; all other pages are static HTML.

***

## Visual principles — global rules

- **Overall style:** civic/investigative. Clean and readable — like a news app, not a SaaS product. Lots of whitespace. No decorative borders.
- **Color:**
  - Light theme only.
  - Background: near-white `#f7f7f8`.
  - Primary accent: `#2563eb` (blue) — used for primary buttons, active filters, search highlights, and the "follow this owner" link.
  - **Corporate flag color:** `#dc2626` (red) — used exclusively for the `CORPORATE` badge and corporate-owned parcel fills. This is a data-meaning color, not decoration.
  - **Institutional flag color:** `#d97706` (amber) — used exclusively for the `INSTITUTIONAL` badge and institutional parcel fills.
  - Neutral grays for everything else. Never more than 4 colors in the chrome at once.
- **Typography:**
  - `system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif` — no web fonts.
  - 3 sizes: page title (1.6–2rem), section heading (1.2–1.4rem), body (1rem).
  - Weight 400 for body, 600 for headings. No italics. Links underlined only when inline in prose.
  - **Site name rendering:** "Who Owns Atlanta?" — always with the question mark. Short tag "whoa?!?" is fine in small UI contexts (favicons, meta tags) but not in body text.
- **Shape:** 4–8px rounding on cards and controls. Shadows only on floating panels over the map (`rgba(15,23,42,0.08)`).
- **Motion:** Micro-transitions only: 150–200ms fade/scale for hover/focus and map fly-to. No loaders or skeleton screens beyond what the browser provides.

***

## Layout patterns

### Map page (`/`) — primary layout

```
+--------------------------------------------------+
|  Who Owns Atlanta?          [address search bar] |
+---------------------------+----------------------+
|                           |                      |
|   VECTOR TILE MAP         |   DETAIL PANEL       |
|   (MapLibre, full height) |   (parcel or owner)  |
|                           |                      |
+--------------------------------------------------+
|  Leaderboard | About | Methodology | FAQ         |
+--------------------------------------------------+
```

- Top bar: site name left, address search center-right, no other controls.
- Map takes all remaining vertical space. Detail panel sits right of map on desktop (fixed ~360px), slides up as a bottom sheet on mobile.
- Detail panel is hidden by default; appears on search selection or map click.
- Bottom nav bar: simple text links, no icons.

### Content pages (About, Methodology, FAQ, Reports)

- Single column, max-width ~720px, centered.
- Same top bar and bottom nav as map page for consistency.
- No map on these pages.

### Leaderboard (`/leaderboard`)

- Single column, max-width ~900px.
- Same top bar and bottom nav.
- Table is the main content; minimal surrounding UI.

### Owner profile (`/owner/<cluster_id>`)

- Two-column on desktop: left ~360px for text/stats, right for a small embedded map showing all parcels in the cluster.
- Single column on mobile.
- Same top bar and bottom nav.

***

## Components

### Address search bar

- Prominent, centered in top bar. Placeholder: `Search an address…`
- Debounced 300ms → `GET /api/search?q=...` → dropdown of up to 8 matches.
- Dropdown items: address string + small county pill (`Fulton` / `DeKalb`).
- On selection: map flies to parcel, parcel highlighted, detail panel opens.

### Detail panel — parcel

Triggered by search selection or map click. Card style, fixed right panel.

Layout (top to bottom):
1. Street address (section heading)
2. Owner name — linked to owner profile if cluster known; plain text otherwise
3. Flag badges: `CORPORATE` (red pill) and/or `INSTITUTIONAL` (amber pill) if flagged
4. Metadata row: neighborhood · NPU · council district (gray, compact)
5. Secondary metadata: land acres · living units · land use code
6. **Permit history section** — collapsed by default, expandable:
   - Summary line: `N complaints (X open)` — last date
   - Expanded: compact list of permits, most recent first

"View full owner profile →" link at bottom, shown only when cluster_id is known.

### Detail panel — owner cluster profile (embedded)

Shown when "View full owner profile" is clicked from parcel panel, or at `/owner/<cluster_id>`.

Sections:
1. **Owner names** — all known names in cluster, stacked, primary name bold
2. **Portfolio stats** — parcel count · total acres · corporate/institutional breakdown (small inline numbers, no charts)
3. **SOS data** (when available) — registered agent · officers · incorporation state — displayed in a `<details>` element, collapsed by default
4. **Parcels list** — compact table: address · county · flags. Clicking a row flies map to that parcel.
5. **Map interaction** — all cluster parcels pulse/highlight while profile is open

### Leaderboard table

Columns: `Rank` · `Owner` · `Parcels` · `Acres` · `Flags`

- `Owner` cell: primary name, smaller secondary names stacked below in gray
- `Flags` cell: `CORPORATE` / `INSTITUTIONAL` pills as applicable
- Each row links to `/owner/<cluster_id>`
- Zebra striping or very light hover — pick one, don't use both

### Ownership flag badges

Two standard badges used throughout:
- `CORPORATE` — red pill (`#dc2626` fill, white text)
- `INSTITUTIONAL` — amber pill (`#d97706` fill, white text)

These must be visually consistent across parcel panel, owner profile, leaderboard, and map tooltips. Define once in CSS, reuse everywhere.

### Map tooltips / hover labels

On parcel hover (zoom 13+): small floating label — address + owner name + flag badges.
Keep it brief; full detail goes in the panel, not the tooltip.

***

## Map-specific guidelines

### Base map

OpenFreeMap vector tileset, desaturated land/water so parcel data layers read clearly against it.

### Parcel color encoding

Two modes, toggled by zoom:

- **Zoom 10–12 (overview):** Fill by ownership type only:
  - Corporate: `#dc2626` at 60% opacity
  - Institutional: `#d97706` at 60% opacity
  - Other: `#94a3b8` (neutral gray) at 40% opacity

- **Zoom 13+ (detail):** Fill by `cluster_id` — consistent hue per cluster so a large landlord's parcels visually pop as a group. Use a deterministic hash → HSL palette (fix saturation ~65%, lightness ~55%) so the same owner always gets the same color.

Do **not** mix size encoding and color encoding simultaneously. Color carries ownership identity; no circle size variations on parcel polygons.

### Active state

Selected parcel: bright blue outline (`#2563eb`, 3px). All other parcels dimmed slightly.

Owner profile open: all cluster parcels get a pulsing blue outline. Non-cluster parcels dimmed.

### Interactions

- Click/tap → select parcel, open detail panel
- Scroll-wheel → zoom
- Drag → pan
- On mobile: map is full-screen; detail panel is a bottom sheet (swipe up to expand)

***

## Content/page-specific notes

### Methodology page

This page matters for credibility. It should explain:
- What "corporate" and "institutional" flags mean (and their known gaps)
- How ownership clustering works (name + address graph)
- Data sources (Fulton/DeKalb tax records, GA SOS bulk download, Accela complaints)
- Known data quirks (Cluster 3 subdivision names, mega-cluster collapsing, "CO" without period)

Plain prose with `<h2>` section headers. No tables required. Link to GitHub if/when the repo is public.

### About page

Short. Mission statement + who built it. Not a wall of text.

### FAQ page

`<details>`/`<summary>` accordion pattern (Pico supports this natively). No JS required.

***

## LLM system/style prompt snippet

Paste this when prompting an LLM to generate HTML/CSS for this project:

> Use Pico.css as the only CSS framework. This is a civic property-data site called "Who Owns Atlanta?".
> Follow a minimalist, investigative-news aesthetic:
> - Light theme, background `#f7f7f8`, primary accent `#2563eb`.
> - `#dc2626` (red) is reserved exclusively for the CORPORATE ownership badge and corporate parcel fills.
> - `#d97706` (amber) is reserved exclusively for the INSTITUTIONAL badge and institutional parcel fills.
> - System sans-serif fonts only. Three text sizes: page title, section heading, body.
> - Map page layout: fixed top bar with site name + address search; below it, full-height MapLibre map with a fixed ~360px detail panel on the right. On mobile, detail panel becomes a bottom sheet.
> - Content pages (About, Methodology, FAQ, Leaderboard): single column, max 720–900px, same top bar and bottom nav.
> - Cards: 4–8px rounding, very light shadow (`rgba(15,23,42,0.08)`). No heavy borders.
> - Buttons: one primary (solid `#2563eb`), one ghost/outline. Text labels; icon-only buttons require `aria-label`.
> - Map: OpenFreeMap vector tileset. Parcels colored by ownership type at low zoom, by cluster_id at zoom 13+. On click, highlight parcel and open detail panel. No mixed color+size encoding.
> - Keep spacing generous, animations subtle (≤ 200ms), and the overall tone journalistic — not flashy.
