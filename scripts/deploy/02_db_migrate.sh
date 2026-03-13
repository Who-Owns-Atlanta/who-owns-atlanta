#!/usr/bin/env bash
# 02_db_migrate.sh — Slim DB dump and restore to VPS
#
# Run from the DEV machine.  Requires woa-1 accessible via SSH.
# The VPS must have Docker running (postgis container up) before step 2.
#
set -euo pipefail

DUMP_FILE="who_owns_atl_prod.dump"
REMOTE="woa-1"
REMOTE_HOME="/home/deploy"
REPO_PATH="$REMOTE_HOME/who-owns-atlanta"

# ---------------------------------------------------------------------------
# Step 1 — on dev: create slim sos_officers table and dump
# ---------------------------------------------------------------------------
echo "=== [DEV] Slimming sos.officers to matched entities only ==="

PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl <<'EOF'
DROP TABLE IF EXISTS public.sos_officers_prod;
CREATE TABLE public.sos_officers_prod AS
SELECT o.* FROM sos.officers o
WHERE o.control_number IN (
    SELECT DISTINCT sos_control_number FROM owner_entities
    WHERE sos_control_number IS NOT NULL
);
EOF

echo "=== [DEV] Dumping DB (public + gis + application; excluding sos/tiger/topology and pipeline-only tables) ==="

PGPASSWORD=woa pg_dump -Fc \
  --exclude-schema=sos \
  --exclude-schema=tiger \
  --exclude-schema=tiger_data \
  --exclude-schema=topology \
  --exclude-table=bq_people \
  --exclude-table=bq_locations \
  --exclude-table=bq_organizations \
  --exclude-table=addr_norm_lookup \
  -h localhost -p 5434 -U woa -d who_owns_atl \
  > "$DUMP_FILE"

echo "Dump size: $(du -sh "$DUMP_FILE" | cut -f1)"

# Clean up slim table from dev
PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl \
  -c "DROP TABLE IF EXISTS public.sos_officers_prod;"

# ---------------------------------------------------------------------------
# Step 2 — transfer to VPS
# ---------------------------------------------------------------------------
echo "=== Transferring dump to $REMOTE ==="
rsync -avz --progress "$DUMP_FILE" "$REMOTE:~/"

# ---------------------------------------------------------------------------
# Step 3 — on VPS: ensure postgis is up, restore, reorganize
# ---------------------------------------------------------------------------
echo "=== [VPS] Starting postgis container ==="
ssh "$REMOTE" "cd $REPO_PATH && docker compose \
    -f docker-compose.yml \
    -f docker-compose.prod.yml \
    up -d postgis"

echo "Waiting 10s for PostgreSQL to be ready..."
sleep 10

echo "=== [VPS] Restoring dump ==="
ssh "$REMOTE" "PGPASSWORD=woa pg_restore \
  -h localhost -p 5434 -U woa -d who_owns_atl \
  --no-owner --role=woa \
  ~/$DUMP_FILE"

echo "=== [VPS] Moving slim table into sos schema ==="
ssh "$REMOTE" "PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl <<'EOF'
CREATE SCHEMA IF NOT EXISTS sos;
ALTER TABLE public.sos_officers_prod SET SCHEMA sos;
ALTER TABLE sos.sos_officers_prod RENAME TO officers;
EOF"

# ---------------------------------------------------------------------------
# Step 4 — recreate materialized views (dropped by CASCADE in build scripts)
# ---------------------------------------------------------------------------
echo "=== [VPS] Recreating materialized views ==="
ssh "$REMOTE" "PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl \
  -f $REPO_PATH/scripts/sql/04_create_materialized_views.sql"

# ---------------------------------------------------------------------------
# Step 5 — verify
# ---------------------------------------------------------------------------
echo "=== [VPS] Verifying row counts ==="
ssh "$REMOTE" "PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl -c \"
SELECT 'leaderboard'    AS view, count(*) FROM mv_leaderboard
UNION ALL
SELECT 'address_search' AS view, count(*) FROM mv_address_search
UNION ALL
SELECT 'cluster_stats'  AS view, count(*) FROM mv_cluster_stats;
\""

# ---------------------------------------------------------------------------
# Step 6 — start API
# ---------------------------------------------------------------------------
echo "=== [VPS] Starting full stack ==="
ssh "$REMOTE" "cd $REPO_PATH && docker compose \
    -f docker-compose.yml \
    -f docker-compose.prod.yml \
    up -d"

echo ""
echo "=== Phase 2 complete ==="
echo "Run verification from Phase 5 of the deployment plan."
