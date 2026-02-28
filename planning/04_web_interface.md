# Plan: Web Interface — Who Owns Atlanta?

**Created:** 2026-02-19
**Status:** Draft

**Site name:** Who Owns Atlanta?
**Short name/tag:** whoa?!?
**Domain:** who-owns-atlanta.org

---

## Goal

A public-facing web interface for exploring Atlanta-area property ownership. Core use case: look up an address, see who owns it, and follow that owner's network across the city.

---

## Data State Assumptions

- **Parcel data:** Fulton (370K) + DeKalb (246K) — fully loaded, flagged, normalized, clustered
- **Permit records:** Accela Building Complaints loaded via `application.records` — full backfill complete
- **SOS data:** Not yet integrated. When it arrives, it enriches existing owner/cluster records — no new UI elements required, existing panels just get more data
- **Static GIS overlays:** neighborhoods, NPU, council districts, address points — all in DB!

---

## Phase 1: Docker Infrastructure

Add new services to `docker-compose.yml` alongside existing `woa_postgis`.

### 1.1 New containers

- **`woa_api`** — FastAPI app (Python/uv). Reads PostGIS. Binds to internal port 8080.

No `woa_tiles` container — tiles are pre-generated static files (see Phase 3), not served dynamically.

No nginx container — the host already runs nginx. Docker containers expose ports only on localhost (`127.0.0.1`); the host nginx proxies to them.

### 1.2 Host nginx configuration

A new server block for `who-owns-atlanta.org`. Key directives:

- Rate limiting: `limit_req_zone` on `/api/` — 10 req/s per IP, burst 30 (defined in `nginx.conf` http block, referenced in vhost)
- Proxy `/api/` → `127.0.0.1:8080`
- Serve static frontend files from a configured root (e.g. `/var/www/who-owns-atlanta/`)
- SSL via Let's Encrypt (Certbot) — production only; dev runs plain HTTP
- Tiles are served from CloudFront/S3 — nginx does not handle tile requests

Rate limiting must live in the host nginx since that's the public-facing layer. Docker-internal nginx would not see real client IPs.

### 1.3 Development workflow

Development avoids full Docker image rebuilds by mounting source code as a volume and running FastAPI with auto-reload. A `dev_rebuild_web.sh` script (adapted from `rebuild_web.sh`) handles this:

- Mounts `./api/` into the container — code changes reload instantly without rebuilding the image
- FastAPI runs with `--reload` flag
- Only a full `docker compose up --build` is needed when dependencies (`pyproject.toml`) change

Local nginx vhost mirrors the prod config **without SSL** — plain `http://` on a local hostname (e.g. `who-owns-atlanta.local` or `who-owns-atlanta.lan` via `/etc/hosts` or local DNS). This keeps dev/prod vhost configs structurally identical; SSL and the real domain are added at deploy time only.

**Note on Hostnames:** The frontend `js/app.js` uses `window.location.hostname` to determine whether to serve tiles from the local `/tiles/` directory or the production CloudFront URL. Currently supported dev hostnames: `who-owns-atlanta.local`, `who-owns-atlanta.lan`, and `localhost`.

Dev vhost config lives in `nginx/who-owns-atlanta.dev.conf` (committed). Prod vhost config lives in `nginx/who-owns-atlanta.conf` (committed, deployed to VPS at Phase 5).

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

Tiles are **pre-generated static `.pbf` files** using tippecanoe, uploaded to S3, and served via CloudFront. No tile server runs on the VPS.

### Why static tiles over pg_tileserv

- **No cold-start DB load:** pg_tileserv fires a PostGIS spatial query per tile per user. On a modest VPS, a user loading the map at zoom 12 triggers 20–60 simultaneous tile requests before any cache warms up.
- **S3/CloudFront is the right cache:** static `.pbf` files map directly to S3 object keys (`/{z}/{x}/{y}.pbf`). CloudFront caches by path natively — tiles never hit the VPS after first fetch.
- **Tile footprint is small:** Atlanta metro at z10–z14 is ~1,300 tiles; output is ~200–500MB, not "several gigs."
- **VPS is out of the tile loop entirely:** only `/api/` calls hit the VPS.

### Tile build process

1. Export GeoJSON from PostGIS (query below)
2. Run tippecanoe to generate a directory of `.pbf` files
3. Sync to S3: `aws s3 sync tiles/ s3://who-owns-atlanta-tiles/ --content-type application/x-protobuf`
4. Invalidate CloudFront on rebuild: `aws cloudfront create-invalidation --paths "/tiles/*"`

```sql
-- GeoJSON export query (produces tile source)
SELECT ST_AsGeoJSON(ST_Transform(geometry, 4326))::json AS geometry,
       parcelid, owner, is_corporate, is_institutional, cluster_id, county
FROM parcels_unified
JOIN owner_entities oe ON parcelid = ANY(oe.parcel_ids);
```

```bash
# tippecanoe build (z10–z14, Atlanta bbox)
tippecanoe -o tiles/ \
  --no-tile-compression \
  --minimum-zoom=10 --maximum-zoom=14 \
  --layer=parcels \
  parcels.geojson
```

### Tile layer behavior

- Zoom 10–12: simplified polygons, color by `is_corporate` / `is_institutional` flag only
- Zoom 13+: full detail, color by `cluster_id` (consistent hue per cluster)
- Client-side: Maplibre GL

### Rebuild trigger

Tiles are stale until rebuilt. Since parcel data and cluster assignments change rarely (monthly pipeline runs at most), a manual rebuild + sync is acceptable. Document the steps in a `scripts/build_tiles.sh` script.

### Traffic architecture

```
User → CloudFront → S3 (static .pbf tiles)        [tiles — VPS never involved]
User → VPS nginx (static files)                    [/, /owner/*, /leaderboard, /about, etc.]
User → VPS nginx → proxy_cache → FastAPI           [/api/search only after cache miss]
User → VPS nginx → proxy_cache (disk hit)          [/api/parcel/, /api/owner/ — FastAPI not needed after first request]
```

---

## Phase 4: Frontend

The site is **not a single-page app**. It has a map-heavy interactive section plus several conventional content pages. Structure:

- **Map/search** (`/`) — JS-heavy, Maplibre GL, dynamic API calls
- **Owner profile** (`/owner/<cluster_id>`) — **pre-generated static HTML** at pipeline time; nginx serves files directly, no FastAPI involved
- **Leaderboard** (`/leaderboard`) — **pre-generated static HTML** at pipeline time; same
- **About** (`/about`) — static HTML (hand-written, checked into repo)
- **Methodology** (`/methodology`) — static HTML (hand-written, checked into repo)
- **FAQ** (`/faq`) — static HTML (hand-written, checked into repo)
- **Reports** (`/reports`) — static generated pages (stretch; e.g. per-neighborhood summaries)

Static content pages and pre-generated pages are all served directly by nginx — no FastAPI for any page load. FastAPI handles only `/api/` calls (primarily address search and parcel detail for the map page). No framework required — vanilla JS for the interactive map only.

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

This is a **pre-generated static HTML page**, not a dynamic API response. Generated at pipeline time by `scripts/build_static_pages.py` and written to the nginx static root as `/owner/<cluster_id>/index.html`. nginx serves it with zero FastAPI/DB involvement.

Displays:
- All known owner names in the cluster
- Total parcels (count + list of all parcels with address/county/flags)
- Total acreage
- Permit activity across all parcels
- SOS data (when available): registered agent, officers, incorporation state
- Link back to map: "View on map →" opens `/` with cluster highlighted

The inline owner summary shown in the map page's detail panel still uses `/api/owner/<cluster_id>` JSON — only the standalone profile page is pre-generated.

### Feature: Leaderboard (`/leaderboard`)

**Pre-generated static HTML page**, written to `/var/www/who-owns-atlanta/leaderboard/index.html` at pipeline time. No API call on page load.

Data source: `mv_leaderboard` materialized view (top 500 clusters by parcel count).
Columns: Rank, Owner name(s), Parcel count, Acreage, Corporate/Institutional flags.
Each row links to the pre-generated `/owner/<cluster_id>` page.

Regenerated by `scripts/build_static_pages.py` after each pipeline run.

### Phase 4b: Static page generation — `scripts/build_static_pages.py`

Runs after each pipeline update. Queries the DB once per cluster (via `mv_cluster_stats` + parcel join), renders HTML using a Jinja2 template, writes to disk.

**Output layout:**
```
/var/www/who-owns-atlanta/
  owner/
    538/index.html
    1042/index.html
    ...  (~42K multi-entity clusters; optionally all 471K)
  leaderboard/
    index.html
```

**Template inputs per cluster:**
- owner_names[], parcel_count, total_acres, corporate_parcel_count
- parcels: [{address, county, owner, is_corporate, is_institutional, lat, lng}]
- permit_summary: {count, open_count, last_date}
- sos_data (if available): {registered_agent, officers[], incorporation_state}

**Scope decision:** Generate pages for all clusters with `parcel_count >= 2` (the interesting ones). Single-parcel clusters are a minority of useful lookup targets and not worth the disk/build time until needed.

**Rebuild trigger:** Same as tile rebuild — manual after pipeline run. Add to a top-level `scripts/rebuild_all.sh` that chains: tiles → static pages → CloudFront invalidation.

**nginx config addition:**
```nginx
# Serve pre-generated owner pages
location /owner/ {
    root /var/www/who-owns-atlanta;
    try_files $uri $uri/index.html =404;
}
```

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
| host nginx | 30MB |
| OS + headroom | 1GB |
| **Total** | **~3.7GB** |

No tile server on VPS — tiles are on S3/CloudFront. `woa_libpostal` is a data-prep tool only; it does not run in production. 8GB gives comfortable buffer for Postgres to cache hot parcel data.

### Caching strategy

Data changes at most monthly (manual pipeline runs). Cache aggressively.

**Static pre-generated pages (zero DB cost):**
- `/owner/<cluster_id>` — pre-generated HTML at pipeline time (see Phase 4b). nginx serves files directly, FastAPI never involved.
- `/leaderboard` — same: pre-generated HTML, nginx serves it.

**HTTP `Cache-Control` headers on all `/api/` GET responses:**
- `/api/parcel/`, `/api/owner/`, `/api/leaderboard` → `Cache-Control: public, max-age=86400`
- `/api/search` → `Cache-Control: no-store` (query-specific, personalized per keystroke)
- Set `Vary: Accept-Encoding` where gzip is used.

**nginx `proxy_cache` for `/api/`:**
- Cache FastAPI JSON responses to disk at the nginx layer. Hits for `/api/parcel/` and `/api/owner/` that miss the browser cache are answered by nginx without touching FastAPI or the DB.
- Cache zone: 100MB on-disk, 1-day TTL, purge on pipeline rebuild.

**Rate limiting:**
- Host nginx: 10 req/s per IP on `/api/` routes, burst 30
- Tile requests: served from CloudFront/S3 — VPS never sees tile traffic

---

## Phased Build Order

1. **Phase 1** — docker-compose addition (`woa_api` stub) + host nginx vhost config ✓ done
2. **Phase 2** — materialized views + FastAPI with `/search`, `/parcel`, `/owner` endpoints + `Cache-Control` headers + nginx `proxy_cache` config
3. **Phase 3** — GeoJSON export → tippecanoe build → S3 upload → CloudFront distribution
4. **Phase 4** — map page: Maplibre GL + address search + parcel detail panel
5. **Phase 4b** — `scripts/build_static_pages.py`: pre-generate owner profile pages + leaderboard → nginx static root
6. **Phase 4c** — static content pages (About, Methodology, FAQ) — hand-written HTML
7. **Phase 5** — VPS deploy, SSL (Let's Encrypt via Certbot), monitoring
8. **Post-launch** — `scripts/rebuild_all.sh` chaining tiles + static pages + CloudFront invalidation

---

## Decisions Made

- **Site name:** Who Owns Atlanta?
- **Domain:** who-owns-atlanta.org (straightforward to change)
- **Auth/admin:** None at launch — publicly readable. Add if/when needed.
- **Choropleth:** Stretch goal — implement after core features are solid
- **Frontend architecture:** Hybrid — JS-heavy map page + conventional server-rendered/static content pages. Not a SPA.
- **nginx:** Host nginx handles public traffic, rate limiting, SSL. No nginx container. Dev vhost is plain HTTP, mirroring prod config structure; SSL added at deploy time only.
- **Dev workflow:** Volume-mounted FastAPI with `--reload`; `dev_rebuild_web.sh` for container restarts. Full image rebuild only when dependencies change.
- **Tile serving:** tippecanoe → static `.pbf` files → S3 + CloudFront. No pg_tileserv. VPS handles only `/api/` traffic.
- **Tile rebuild:** manual trigger via `scripts/build_tiles.sh` after pipeline runs. Parcel data changes rarely so staleness is not a concern.
- **Owner profile pages:** pre-generated static HTML via `scripts/build_static_pages.py`. Scope: all clusters with `parcel_count >= 2`. nginx serves directly — zero FastAPI/DB cost per page load.
- **Leaderboard page:** pre-generated static HTML at same pipeline step. Source: `mv_leaderboard`.
- **Caching:** `Cache-Control: public, max-age=86400` on all stable `/api/` GET responses. nginx `proxy_cache` for remaining API traffic. `/api/search` is `no-store`.
- **No in-process LRU cache:** not needed given pre-generation + nginx proxy_cache coverage.
