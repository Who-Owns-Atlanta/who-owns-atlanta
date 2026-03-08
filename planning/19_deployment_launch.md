# Deployment & Launch Plan — Who Owns Atlanta

**Created:** 2026-03-07
**Status:** Ready to execute

---

## Known State

- VPS: `ssh woa-1` (Tailscale), Ubuntu 24.04 LTS, IP 216.106.177.76
- Domain: `who-owns-atlanta.org` — registered and managed in Cloudflare
- GitHub org: https://github.com/Who-Owns-Atlanta (created)
- Email: info@who-owns-atlanta.org
- Cloudflare credentials: `~/.secrets/cloudflare.ini` (`dns_cloudflare_api_token`)
- Root SSH access confirmed on VPS

---

## Corrections vs. Original launch_plan

| Item | launch_plan says | Reality |
|---|---|---|
| Static HTML count | "~850MB for 42K clusters" | 135,594 owner dirs, 1.3GB |
| Tile size | 270MB | 198MB (duplicate tiles resolved) |
| SOS schema size | 11.5GB | 14GB |
| DB dump command | `pg_dump -Fc` (full 17GB) | Must slim `sos.*` first — see §DB |
| `sos.officers` in prod? | not addressed | **YES** — `/api/owner/` JOINs it directly (`main.py:262`) |
| proxy_cache | described in plan docs | **not in actual `who-owns-atlanta.conf`** — add it |
| CORS | not mentioned | currently `allow_origins=["*"]` — must restrict |
| Materialized views on VPS | "ensure indexes are warmed" | Views are **dropped by CASCADE** in script 10 — must recreate, not refresh |

---

## Testing Without Enabling DNS

Domain is in Cloudflare already. Use certbot's DNS challenge — Cloudflare API adds the TXT
record automatically. VPS does not need to be reachable on port 80.

```bash
# Copy credentials to VPS (one-time)
scp ~/.secrets/cloudflare.ini woa-1:/root/.cloudflare.ini
ssh woa-1 chmod 600 /root/.cloudflare.ini

# On VPS — get cert
certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /root/.cloudflare.ini \
  --email info@who-owns-atlanta.org \
  --agree-tos --non-interactive \
  -d who-owns-atlanta.org
```

Test the full site by adding to your **local** `/etc/hosts`:
```
216.106.177.76  who-owns-atlanta.org
```
Browser hits VPS directly over HTTPS. Cloudflare is not yet involved.
Remove this entry and flip DNS when satisfied.

---

## Git Repo Strategy

**Transfer repo to org FIRST (`jessedp/who_owns_atl` → `Who-Owns-Atlanta/who-owns-atlanta`).**
The setup script clones from `https://github.com/Who-Owns-Atlanta/who-owns-atlanta`.
If the repo is private until launch, use a read-only deploy key.

**What's in git vs. what's rsynced:**
- In git: `web/api/`, `web/nginx/`, `web/frontend/` JS/CSS/HTML, `docker-compose*.yml`, `scripts/`
- Rsynced from dev: `owner/`, `leaderboard/`, `l/`, `tiles/`, `img/`, `agent/`, `agents/`, `addresses/`, `numbers/`

---

## Phase 0 — Pre-deployment Code Changes

Make these changes in the repo before any server work. Commit to `Who-Owns-Atlanta/who-owns-atlanta`.

- [ ] **`web/api/main.py` line 10** — CORS: `allow_origins=["*"]` → `allow_origins=["https://who-owns-atlanta.org"]`
- [ ] **`scripts/build_static_pages.py`** — Add `autoescape=True` to Jinja2 `Environment()` call
- [ ] **`web/frontend/js/app.js`** — Set `PROD_TILES_URL = "https://tiles.who-owns-atlanta.org"`
- [ ] **`web/nginx/who-owns-atlanta.conf`** — Add security headers to `server` block:
  ```nginx
  add_header X-Content-Type-Options nosniff always;
  add_header X-Frame-Options DENY always;
  add_header Referrer-Policy strict-origin-when-cross-origin always;
  ```
  And add `proxy_cache` directives for `/api/parcel/` and `/api/owner/`
- [ ] **`docker-compose.prod.yml`** (new file) — Production overrides:
  ```yaml
  services:
    api:
      environment:
        DEV_MODE: "0"
      command: uv run uvicorn main:app --host 0.0.0.0 --port 8080 --workers 2
      volumes: []
  ```
- [ ] Commit + push to `Who-Owns-Atlanta/who-owns-atlanta`

---

## Phase 1 — Server Setup (`scripts/deploy/01_server_setup.sh`)

Run as root on `woa-1`. Creates deploy user, installs packages, configures nginx and Docker.

```bash
# Run as: ssh woa-1 'bash -s' < scripts/deploy/01_server_setup.sh
```

Script actions:
- Create `deploy` user, add to `docker` group, copy root's authorized_keys
- Install: nginx, docker-ce (official repo), python3-certbot-dns-cloudflare, uv, netdata
- Configure ufw: allow 22/tcp, 80/tcp, 443/tcp; enable
- Add to `/etc/nginx/nginx.conf` http block:
  ```nginx
  limit_req_zone $binary_remote_addr zone=woa_api:10m rate=10r/s;
  proxy_cache_path /var/cache/nginx/woa levels=1:2 keys_zone=woa_cache:10m
                   max_size=100m inactive=1d use_temp_path=off;
  ```
- Create `/var/www/who-owns-atlanta/{owner,tiles,leaderboard,l,img,agent,agents,addresses,numbers}/`
- `chown -R deploy:deploy /var/www/who-owns-atlanta/`
- Clone repo: `git clone https://github.com/Who-Owns-Atlanta/who-owns-atlanta /home/deploy/who-owns-atlanta`
- Copy `web/nginx/who-owns-atlanta.conf` → `/etc/nginx/sites-available/`; enable site
- `nginx -t && systemctl reload nginx`
- Certbot DNS challenge (uses `/root/.cloudflare.ini` — must be copied before running)
- `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`

---

## Phase 2 — Database Migration (`scripts/deploy/02_db_migrate.sh`)

**DB size problem**: `sos.officers` is 7.97GB (49M rows) but the API only needs officers
for matched entities (a few thousand rows). Slim it first.

```bash
# --- On DEV machine ---

# Step 1: Create slim copy of sos.officers
PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl <<'EOF'
DROP TABLE IF EXISTS public.sos_officers_prod;
CREATE TABLE public.sos_officers_prod AS
SELECT o.* FROM sos.officers o
WHERE o.control_number IN (
    SELECT DISTINCT sos_control_number FROM owner_entities
    WHERE sos_control_number IS NOT NULL
);
EOF

# Step 2: Dump without sos.*, tiger, topology; include the slim table
PGPASSWORD=woa pg_dump -Fc \
  --exclude-schema=sos --exclude-schema=tiger --exclude-schema=topology \
  -t public.sos_officers_prod \
  -h localhost -p 5434 -U woa who_owns_atl \
  > who_owns_atl_prod.dump
# Expected: ~2.6GB (vs 17GB full dump)

# Step 3: Transfer
rsync -avz --progress who_owns_atl_prod.dump woa-1:~/

# --- On VPS (woa-1) ---

# Step 4: Restore
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d postgis
sleep 10  # wait for postgres ready

PGPASSWORD=woa pg_restore -h localhost -p 5434 -U woa -d who_owns_atl \
  --no-owner --role=woa ~/who_owns_atl_prod.dump

# Step 5: Move slim officers table into sos schema
PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl <<'EOF'
CREATE SCHEMA IF NOT EXISTS sos;
ALTER TABLE public.sos_officers_prod SET SCHEMA sos;
ALTER TABLE sos.sos_officers_prod RENAME TO officers;
EOF

# Step 6: Recreate materialized views (dropped by CASCADE — must create, not refresh)
PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl \
  -f /home/deploy/who-owns-atlanta/scripts/sql/04_create_materialized_views.sql

# Step 7: Verify
PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl -c "
  SELECT 'leaderboard' AS view, count(*) FROM mv_leaderboard
  UNION ALL SELECT 'address_search', count(*) FROM mv_address_search
  UNION ALL SELECT 'cluster_stats',  count(*) FROM mv_cluster_stats;"
```

---

## Phase 3 — Cloudflare R2 (Tiles CDN)

Tiles (198MB) go to R2 — well within free tier (10GB).

```bash
# Install wrangler on dev machine (or VPS)
npm install -g wrangler

export CLOUDFLARE_API_TOKEN=$(grep api_token ~/.secrets/cloudflare.ini | cut -d= -f2 | tr -d ' ')

# Create bucket
wrangler r2 bucket create who-owns-atlanta-tiles

# Upload tiles using rclone (easier for directories than wrangler)
# Configure rclone R2 remote using the same API token + account ID from Cloudflare dashboard
rclone copy /var/www/who-owns-atlanta/tiles/ \
  r2:who-owns-atlanta-tiles \
  --s3-content-type application/x-protobuf

# In Cloudflare dashboard (or via API):
# - Set custom domain: tiles.who-owns-atlanta.org → who-owns-atlanta-tiles bucket
# - Add Cache Rule: tiles.who-owns-atlanta.org/* → Cache Everything, TTL 30 days
```

---

## Phase 4 — Rsync Static Assets (Dev → VPS)

```bash
REMOTE="woa-1:/var/www/who-owns-atlanta"

rsync -avz --progress /var/www/who-owns-atlanta/owner/       $REMOTE/owner/
rsync -avz --progress /var/www/who-owns-atlanta/leaderboard/ $REMOTE/leaderboard/
rsync -avz --progress /var/www/who-owns-atlanta/l/           $REMOTE/l/
rsync -avz --progress /var/www/who-owns-atlanta/agent/       $REMOTE/agent/
rsync -avz --progress /var/www/who-owns-atlanta/agents/      $REMOTE/agents/
rsync -avz --progress /var/www/who-owns-atlanta/addresses/   $REMOTE/addresses/
rsync -avz --progress /var/www/who-owns-atlanta/numbers/     $REMOTE/numbers/
rsync -avz --progress /var/www/who-owns-atlanta/frontend/img/ $REMOTE/frontend/img/
# Tiles → R2 (Phase 3), not to VPS disk
```

`frontend/` JS/CSS/HTML comes from the git clone, not rsync.

---

## Phase 5 — Verification (Local /etc/hosts test)

Add `216.106.177.76  who-owns-atlanta.org` to local `/etc/hosts`, then:

```bash
# API
curl -s https://who-owns-atlanta.org/api/health
curl -s "https://who-owns-atlanta.org/api/search?q=123+PEACHTREE"
curl -s "https://who-owns-atlanta.org/api/parcel/fulton/14F0070LL0380" | python3 -m json.tool | head -20
curl -s "https://who-owns-atlanta.org/api/owner/8" | python3 -m json.tool | head -20

# Static pages (served by nginx, no FastAPI)
curl -sI https://who-owns-atlanta.org/owner/8/
curl -sI https://who-owns-atlanta.org/leaderboard/
curl -sI https://who-owns-atlanta.org/

# Tiles from R2
curl -sI https://tiles.who-owns-atlanta.org/12/1086/700.pbf

# Security headers
curl -sI https://who-owns-atlanta.org/ | grep -i "x-content-type\|x-frame\|referrer"

# CORS (should see no Allow-Origin for untrusted origin)
curl -sI -H "Origin: https://evil.example.com" \
  "https://who-owns-atlanta.org/api/search?q=ABC" | grep -i access-control
```

When all green: remove the `/etc/hosts` entry, proceed to DNS cutover.

---

## Phase 6 — DNS Cutover

All in Cloudflare (scripted via API using `~/.secrets/cloudflare.ini` token, or dashboard):

- [ ] Set A record `who-owns-atlanta.org` → `216.106.177.76`, **proxied** (orange cloud)
- [ ] SSL/TLS mode: **Full (Strict)**
- [ ] Cache Rule: `who-owns-atlanta.org/owner/*` → Cache Everything, TTL 1 day
- [ ] Remove `216.106.177.76 who-owns-atlanta.org` from local `/etc/hosts`
- [ ] Verify from external: `curl -sI https://who-owns-atlanta.org/api/health`

---

## Phase 7 — Monitoring

- **Netdata**: installed via Phase 1; access via `ssh -L 19999:localhost:19999 woa-1` → http://localhost:19999
- **UptimeRobot** (free): monitor `https://who-owns-atlanta.org/api/health`, 5-min interval, alert to `info@who-owns-atlanta.org`
- **Cloudflare Analytics**: free dashboard — traffic, cache hit rate, top paths, DDoS events. Zero page-script.
- **Umami** (optional, later): self-hosted Docker analytics if page-level stats wanted

---

## Phase 8 — GitHub & Launch

**Repo checklist:**
- [ ] Repo transferred to `https://github.com/Who-Owns-Atlanta/who-owns-atlanta`
- [ ] Private until launch day, then flip to public
- [ ] README: pipeline stats, data sources, methodology, city replication guide

**Social sequence (day-of):**
1. Make repo public
2. Bluesky: post site + repo, tag Atlanta civic/housing accounts (Atlanta Civic Circle, Urbanize Atlanta, Georgia Recorder)
3. Reddit r/Atlanta: lead with data story, not the code. Numbers to highlight: Amherst 2,490 parcels, FirstKey 1,224, Progress 560, Invitation Homes 2,832

---

## Future: A/B Database Pattern

For data refreshes, use blue/green DB containers:

```yaml
# Add to docker-compose.prod.yml when refreshing
services:
  postgis_green:
    image: postgis/postgis:16-3.4
    container_name: woa_postgis_green
    volumes:
      - woa_pgdata_green:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5435:5432"
```

Restore into green, validate, flip `DATABASE_URL` in `.env`, `docker compose restart api`. Zero downtime.

---

## Files to Create/Modify (Summary)

| File | Action |
|---|---|
| `web/api/main.py` | CORS: `["*"]` → `["https://who-owns-atlanta.org"]` |
| `scripts/build_static_pages.py` | Jinja2 `autoescape=True` |
| `web/frontend/js/app.js` | `PROD_TILES_URL = "https://tiles.who-owns-atlanta.org"` |
| `web/nginx/who-owns-atlanta.conf` | Security headers + proxy_cache |
| `docker-compose.prod.yml` | New — prod overrides |
| `scripts/deploy/01_server_setup.sh` | New — VPS provisioning |
| `scripts/deploy/02_db_migrate.sh` | New — slim dump + restore |
