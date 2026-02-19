# Plan: Web Interface — Who Owns Atlanta?

**Created:** 2026-02-19
**Status:** Draft

**Site name:** Who Owns Atlanta?
**Domain:** who-owns-atlanta.org

---

## Goal

A public-facing web interface for exploring Atlanta-area property ownership. Core use case: look up an address, see who owns it, and follow that owner's network across the city.

---

## Data State Assumptions

- **Parcel data:** Fulton (370K) + DeKalb (246K) — fully loaded, flagged, normalized, clustered
- **Permit records:** Accela Building Complaints loaded via `application.records` — full backfill complete
- **SOS data:** Not yet integrated. When it arrives, it enriches existing owner/cluster records — no new UI elements required, existing panels just get more data
- **Static GIS overlays:** neighborhoods, NPU, council districts, address points — all in DB

---

## Phase 1: Docker Infrastructure

Add new services to `docker-compose.yml` alongside existing `woa_postgis` and `woa_libpostal`.

### 1.1 New containers

- **`woa_api`** — FastAPI app (Python/uv). Reads PostGIS. Binds to internal port 8080.
- **`woa_tiles`** — `pg_tileserv` (official `pramsey/pg_tileserv` image). Serves vector tiles directly from PostGIS. Binds to internal port 7800.

No nginx container — the host already runs nginx. Docker containers expose ports only on localhost (`127.0.0.1`); the host nginx proxies to them.

### 1.2 pg_tileserv setup

- Point at `woa_postgis` via env var `DATABASE_URL`
- Expose the `mv_parcels_tile` materialized view (see Phase 3)
- Config: restrict to read-only tile endpoints only
- Tiles served as `/{layer}/{z}/{x}/{y}.pbf`

### 1.3 Host nginx configuration

A new server block for `who-owns-atlanta.org`. Key directives:

- Rate limiting: `limit_req_zone` on `/api/` — 10 req/s per IP, burst 30 (defined in `nginx.conf` http block, referenced in vhost)
- Proxy `/api/` → `127.0.0.1:8080`
- Proxy `/tiles/` → `127.0.0.1:7800`
- Cache headers: tile responses get `Cache-Control: public, max-age=86400`
- Serve static frontend files from a configured root (e.g. `/var/www/who-owns-atlanta/`)
- SSL via Let's Encrypt (Certbot)

Rate limiting must live in the host nginx since that's the public-facing layer. Docker-internal nginx would not see real client IPs.

### 1.4 Environment / secrets

- Single `.env` shared into containers (already exists, has Accela creds)
- Add `DB_URL` pointing at `woa_postgis:5432` (internal Docker network)

---

## Phase 2: API Server (FastAPI)

Thin read-only API. No writes. All heavy aggregations are precomputed (materialized views).

### Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/search?q=<address>` | Address autocomplete (top 8, debounced client-side) |
| GET | `/api/parcel/<county>/<parcel_id>` | Full parcel detail |
| GET | `/api/owner/<cluster_id>` | Owner cluster profile — all parcels, stats |
| GET | `/api/leaderboard` | Top N clusters by parcel count (from mat. view) |
| GET | `/api/health` | Liveness check |

### Address search notes

- Query `gis."Address_Point"` (262K points, already loaded) — fastest for typeahead
- Try plain `ILIKE` prefix match first; libpostal normalization adds latency and may not be worth it for autocomplete
- Return: address string, parcel_id, county, lat/lng for map fly-to

### Parcel detail response

```
parcel_id, county, address, owner, is_corporate, is_institutional,
cluster_id, land_acres, living_units, lucode/usecd,
neighborhood, npu, council_district (if available),
permit_count, open_permits, last_permit_date
```

Note: neighborhood/NPU/council come from `view_records_with_parcels` (Tax_Parcel bridge) for Accela-linked parcels. Direct spatial join from `gis` schema overlays is the fallback.

### Owner cluster profile response

```
cluster_id, parcel_count, entity_count, owner_names[],
total_land_acres, corporate_parcel_count,
parcels: [{parcel_id, county, address, owner, lat, lng}, ...]
```

SOS enrichment (when available) adds: registered_agent, officers[], principal_address, incorporation_state — no endpoint changes needed.

### Materialized views to create

- `mv_cluster_stats` — parcel count, acreage, permit count per cluster_id. Refresh nightly.
- `mv_leaderboard` — top 500 clusters by parcel count. Refresh nightly.
- `mv_parcel_permits` — per-parcel complaint count/stats joined from `application.records`.

---

## Phase 3: Vector Tile Layer

pg_tileserv serves any PostGIS table or function directly. Create a dedicated materialized view for tiles to control exactly what properties are encoded and eliminate per-request join cost.

### Tile source: `public.mv_parcels_tile` (materialized)

```sql
-- Combines both counties, joins cluster_id, exposes only tile-relevant columns
SELECT geometry, parcelid, owner, is_corporate, is_institutional,
       cluster_id, county
FROM fulton_parcels + owner_entities join
UNION ALL
FROM dekalb_parcels + owner_entities join
```

(Full query already drafted. Uses `parcelid = ANY(oe.parcel_ids)` join — verified 1:1 per parcel.)

### Tile layer behavior

- Zoom 10–12: simplified polygons, color by `is_corporate` / `is_institutional` flag only
- Zoom 13+: full detail, color by `cluster_id` (consistent hue per cluster)
- Client-side: Maplibre GL or Leaflet.VectorGrid for rendering

### Why pg_tileserv over pre-generated tiles

- No export/re-export step when data changes
- cluster_id and flags update in-place in PostGIS
- ~30MB RAM footprint, negligible CPU at this traffic scale
- Can switch to pre-generated tippecanoe tiles later if serving costs become an issue

### Materialized view decision

**Materialize `mv_parcels_tile`, not `parcels_unified`.**

`parcels_unified` is a pure `UNION ALL` — Postgres splits tile queries into two GIST index scans (one per county table), which is already fast. No need to duplicate it.

The tile view adds a `JOIN owner_entities` to get `cluster_id` via `parcelid = ANY(oe.parcel_ids)` — an array containment scan across 543K rows, executed on every tile request. Pre-joining this into a materialized view with a single GIST index eliminates that per-request cost entirely.

Downside is minimal: parcel data is essentially static, so staleness isn't a concern. Refresh with `REFRESH MATERIALIZED VIEW CONCURRENTLY` (non-blocking, takes a few minutes) after any data update. Extra disk ~1–2GB.

---

## Phase 4: Frontend

The site is **not a single-page app**. It has a map-heavy interactive section plus several conventional content pages. Structure:

- **Map/search** (`/`) — JS-heavy, Maplibre GL, dynamic API calls
- **Owner profile** (`/owner/<cluster_id>`) — can be server-rendered or JS, links back to map
- **Leaderboard** (`/leaderboard`) — mostly static table, minimal JS
- **About** (`/about`) — static HTML
- **Methodology** (`/methodology`) — static HTML, explains data sources, flagging logic, known gaps
- **FAQ** (`/faq`) — static HTML
- **Reports** (`/reports`) — static generated pages (stretch; e.g. per-neighborhood summaries)

Static content pages are plain HTML served directly by nginx. The map page and owner profile load data from the API. No framework required — vanilla JS for the interactive parts, keeping the stack simple and the site fast.

### Map page layout

```
+------------------------------------------+
|  Who Owns Atlanta?   [search bar]        |
+------------------+-----------------------+
|                  |                       |
|   MAP            |   DETAIL PANEL        |
|   (vector tiles) |   (parcel or owner)   |
|                  |                       |
+------------------------------------------+
|  nav: Leaderboard | About | Methodology  |
+------------------------------------------+
```

### Feature: Address search

1. User types address → debounced 300ms → `GET /api/search?q=...`
2. Dropdown shows up to 8 matches
3. User selects → map flies to parcel → parcel outline highlighted → detail panel loads

### Feature: Parcel detail panel

Triggered by address search selection or clicking a parcel on the map.

Displays:
- Street address
- Owner name (linked to owner profile if cluster known)
- Corporate / institutional badge if flagged
- Neighborhood, NPU, council district
- Land acres, living units, land use code
- Permit history: count, open/closed, most recent date (expandable list)

### Feature: Owner cluster profile

Accessible from parcel panel ("View full owner profile") or directly at `/owner/<cluster_id>`.

Displays:
- All known owner names in the cluster
- Total parcels (count + map highlight of all parcels in cluster)
- Total acreage
- Permit activity across all parcels
- SOS data (when available): registered agent, officers, incorporation state

Map highlight: all parcels in the cluster are highlighted/pulsed when profile is open.

### Feature: Leaderboard (`/leaderboard`)

Loaded from `GET /api/leaderboard` (materialized view, no join cost).

Columns: Rank, Owner name(s), Parcel count, Acreage, Corporate/Institutional flags.
Each row links to the owner cluster profile.

### Feature: Corporate ownership choropleth (stretch)

Aggregate `is_corporate` parcel count by neighborhood polygon. Render as a fill layer toggle on the map. Data from a materialized view — single GeoJSON endpoint, cached.

---

## Phase 5: Deployment / VPS

### Minimum viable VPS

- **2 vCPU / 8GB RAM** — comfortable for this workload
- 40GB SSD — 2GB DB now + room to grow, OS, logs
- ~$20-40/mo (Hetzner CX32 or equivalent)

### RAM budget at idle

| Service | Est. RAM |
|---|---|
| woa_postgis (shared_buffers=2GB) | 2.5GB |
| woa_api (FastAPI + workers) | 200MB |
| woa_tiles (pg_tileserv) | 50MB |
| woa_libpostal | 200MB |
| host nginx | 30MB |
| OS + headroom | 1GB |
| **Total** | **~4GB** |

8GB gives comfortable buffer for Postgres to cache hot parcel data.

### Rate limiting strategy

- Host nginx: 10 req/s per IP on `/api/` routes, burst 30
- Tile requests: CDN-cached by z/x/y — doesn't re-hit pg_tileserv on repeat
- Leaderboard: materialized view, no join cost
- `/api/owner/<cluster_id>` is the heaviest endpoint — consider in-process LRU cache for top-1000 clusters (5 min TTL)

---

## Phased Build Order

1. **Phase 1** — docker-compose additions (`woa_api` stub + `woa_tiles`) + host nginx vhost config
2. **Phase 2** — materialized views + FastAPI with `/search` and `/parcel` endpoints
3. **Phase 3** — `mv_parcels_tile` materialized view + verify pg_tileserv serving it
4. **Phase 4** — map page: Maplibre GL + address search + parcel detail panel
5. **Phase 4b** — owner cluster profile page + map highlight
6. **Phase 4c** — leaderboard page
7. **Phase 4d** — static content pages (About, Methodology, FAQ)
8. **Phase 5** — VPS deploy, SSL (Let's Encrypt via Certbot), monitoring

---

## Decisions Made

- **Site name:** Who Owns Atlanta?
- **Domain:** who-owns-atlanta.org (straightforward to change)
- **Auth/admin:** None at launch — publicly readable. Add if/when needed.
- **Choropleth:** Stretch goal — implement after core features are solid
- **Frontend architecture:** Hybrid — JS-heavy map page + conventional server-rendered/static content pages. Not a SPA.
- **nginx:** Host nginx (already running) handles public traffic, rate limiting, SSL. No nginx container.
- **Materialized view:** `mv_parcels_tile` only (tile view with cluster_id join); `parcels_unified` stays a plain view.
