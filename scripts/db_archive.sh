#!/usr/bin/env bash
# Restore a dump into a versioned named database.
# Usage: scripts/db_archive.sh <version>   e.g.  scripts/db_archive.sh v202603A.1
set -euo pipefail

VERSION="${1:-}"
[[ -z "$VERSION" ]] && { echo "Usage: $0 <version>"; exit 1; }

DUMP_FILE="dumps/${VERSION}.dump"
[[ ! -f "$DUMP_FILE" ]] && { echo "ERROR: $DUMP_FILE not found"; exit 1; }

DBNAME="woa_$(echo "$VERSION" | tr '[:upper:]' '[:lower:]' | tr -d '.')"
export PGPASSWORD="woa"
PG="-h localhost -p 5434 -U woa"

DB_EXISTS=$(psql $PG -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$DBNAME';")
if [[ "$DB_EXISTS" == "1" ]]; then
    read -rp "$DBNAME already exists. Drop and recreate? [y/N] " C
    [[ "${C,,}" != "y" ]] && { echo "Aborted."; exit 0; }
    psql $PG -d postgres -c "DROP DATABASE $DBNAME;"
fi

psql $PG -d postgres -c "CREATE DATABASE $DBNAME TEMPLATE template0 OWNER woa;"

# Extensions must pre-exist before pg_restore (dump has spatial_ref_sys rows but not CREATE EXTENSION)
psql $PG -d "$DBNAME" <<'EOSQL'
CREATE SCHEMA IF NOT EXISTS tiger;
CREATE SCHEMA IF NOT EXISTS tiger_data;
CREATE SCHEMA IF NOT EXISTS topology;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology WITH SCHEMA topology;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch CASCADE;
CREATE EXTENSION IF NOT EXISTS postgis_tiger_geocoder WITH SCHEMA tiger;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS address_standardizer;
EOSQL

pg_restore $PG -d "$DBNAME" --no-owner --role=woa --exit-on-error "$DUMP_FILE"

psql $PG -d "$DBNAME" -c "
SELECT
  (SELECT COUNT(*) FROM ownership_clusters)          AS clusters,
  (SELECT SUM(parcel_count) FROM ownership_clusters) AS parcels,
  (SELECT COUNT(*) FROM fulton_parcels)               AS fulton,
  (SELECT COUNT(*) FROM dekalb_parcels)               AS dekalb;"

echo ""
echo "Done: $DBNAME available."
echo "Compare: uv run scripts/compare_releases.py who_owns_atl $DBNAME"
