# Production Runbook — Who Owns Atlanta?

**Created:** 2026-02-20

---

## Rebuild dependency map

Not all data changes require a full rebuild. Match the change to the minimum required steps.

| What changed | Steps required |
|---|---|
| Parcel data (new county export) | pipeline → tiles → static pages |
| Ownership clustering (re-run scripts 04+) | tiles → static pages |
| Permit records only | permit refresh → mat view refresh → static pages |
| SOS data only | mat view refresh → static pages |
| HTML template / design change | static pages only |
| API code change | restart `woa_api` container only |
| nginx config change | `nginx -s reload` only |

---

## Permit-only update

Permits live in `application.records`, updated independently of the parcel pipeline via `scripts/06_pull_accela_records.py`. A permit update does **not** require a tile rebuild or parcel pipeline re-run.

```bash
# 1. Pull new permit records
uv run scripts/06_pull_accela_records.py

# 2. Refresh the permit materialized view
PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl \
  -c "REFRESH MATERIALIZED VIEW mv_parcel_permits;"

# 3. Regenerate static pages (picks up new permit counts)
uv run scripts/build_static_pages.py
```

No tile rebuild. No parcel pipeline. No schema changes.

---

## Full rebuild (after parcel pipeline run)

```bash
# After running scripts 01–04 (or 01–11):
scripts/rebuild_all.sh
```

`rebuild_all.sh` (to be written at Phase 5) chains:
1. GeoJSON export + tippecanoe → S3 tile sync + CloudFront invalidation
2. `build_static_pages.py` — regenerates all owner profile + leaderboard HTML
3. `REFRESH MATERIALIZED VIEW` for all three mat views

---

## Frontend Production Deployment

Before deploying the frontend to production, perform these manual verification steps:

1. **Set Production Tile URL**: Update `PROD_TILES_URL` in `web/frontend/js/app.js` with the live CloudFront distribution URL (e.g. `https://tiles.who-owns-atlanta.org/tiles/{z}/{x}/{y}.pbf`).
2. **Verify Hostname Logic**: Ensure the production domain is **not** included in the `DEV_HOSTNAMES` array in `app.js` to avoid broken tile requests.
3. **API CORS Policy**: The FastAPI `woa_api` currentlly allows all origins (`*`). Review this for production and consider restricting to `who-owns-atlanta.org`.

---

## Materialized view refresh schedule

| View | Refresh trigger |
|---|---|
| `mv_parcel_permits` | After any permit pull |
| `mv_cluster_stats` | After parcel pipeline or clustering re-run |
| `mv_leaderboard` | After parcel pipeline or clustering re-run |
