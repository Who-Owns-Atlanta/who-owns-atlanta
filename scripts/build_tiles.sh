#!/usr/bin/env bash
# Build parcel vector tiles for Who Owns Atlanta?
#
# Usage:
#   scripts/build_tiles.sh [--output-dir DIR]
#
# Defaults:
#   --output-dir  /var/www/who-owns-atlanta/tiles
#
# Prod:  after building, sync to S3 and invalidate CloudFront:
#   aws s3 sync "$OUTPUT_DIR" s3://who-owns-atlanta-tiles/ \
#       --content-type application/x-protobuf --delete
#   aws cloudfront create-invalidation \
#       --distribution-id $CF_DIST_ID --paths "/tiles/*"

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OUTPUT_DIR="/var/www/who-owns-atlanta/tiles"
DB_HOST="localhost"
DB_PORT="5434"
DB_NAME="who_owns_atl"
DB_USER="woa"
DB_PASS="woa"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="$(mktemp -d)"

psql_cmd() {
  PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" "$@"
}

cleanup() {
  rm -rf "$WORK_DIR"
  psql_cmd -c "DROP TABLE IF EXISTS _tile_oe_map;" 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "$OUTPUT_DIR"

# ---------------------------------------------------------------------------
# Steps 1 + 2: Export GeoJSON and build tiles in one pipeline
# ---------------------------------------------------------------------------
# ogr2ogr writes to stdout (/vsistdout/); tippecanoe reads from stdin (-).
# Both processes run concurrently — no intermediate file written to disk.
#
# Zoom 10–14:
#   z10–12: simplified overview — color by is_corporate/is_institutional
#   z13–14: full detail — color by cluster_id
#
# --read-parallel: tippecanoe parses stdin features across multiple threads.
# --coalesce-densest-as-needed: merge features at low zoom rather than drop.
# --no-tile-compression: raw .pbf — nginx/CloudFront handle Content-Encoding.

# ---------------------------------------------------------------------------
# Step 1: Materialize unnested parcel→cluster mapping for an efficient join
# ---------------------------------------------------------------------------
# Unnesting owner_entities.parcel_ids inline at query time forces PostgreSQL
# into a Merge Right Join with full sorts on both sides (~20 min for 615K rows).
# Materializing once as a real table lets the planner choose Hash Left Join
# (~1.4s for the full export query).

echo "==> Materializing parcel→cluster map..."
psql_cmd -c "
  DROP TABLE IF EXISTS _tile_oe_map;
  CREATE TABLE _tile_oe_map AS
    SELECT unnest(parcel_ids) AS parcel_id, county, cluster_id
    FROM owner_entities;
  CREATE INDEX ON _tile_oe_map (parcel_id, county);
  ANALYZE _tile_oe_map;
"

echo "==> Exporting from PostGIS and building tiles (parallel pipeline)..."

TILE_TMP="$WORK_DIR/tiles"

tippecanoe \
  --output-to-directory "$TILE_TMP" \
  --no-tile-compression \
  --minimum-zoom=10 \
  --maximum-zoom=14 \
  --layer=parcels \
  --attribute-type=is_corporate:bool \
  --attribute-type=is_institutional:bool \
  --coalesce-densest-as-needed \
  --quiet \
  <(PGPASSWORD="$DB_PASS" ogr2ogr \
      -f GeoJSON /vsistdout/ \
      "PG:host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USER password=$DB_PASS" \
      -sql "
        SELECT
            p.geometry,
            p.parcel_id,
            p.county,
            p.is_corporate::int     AS is_corporate,
            p.is_institutional::int AS is_institutional,
            m.cluster_id
        FROM parcels_unified p
        LEFT JOIN _tile_oe_map m
          ON m.parcel_id = p.parcel_id AND m.county = p.county
      " \
      -nln parcels \
      -lco COORDINATE_PRECISION=6)

TILE_COUNT=$(find "$TILE_TMP" -name "*.pbf" | wc -l)
TILE_SIZE=$(du -sh "$TILE_TMP" | cut -f1)
echo "    Built $TILE_COUNT tiles ($TILE_SIZE)"

# ---------------------------------------------------------------------------
# Step 3: Swap into place atomically
# ---------------------------------------------------------------------------
# Move old tiles aside, swap new ones in, remove old.

echo "==> Installing tiles to $OUTPUT_DIR..."

OLD_DIR="${OUTPUT_DIR}.old"
rm -rf "$OLD_DIR"
[ -d "$OUTPUT_DIR" ] && mv "$OUTPUT_DIR" "$OLD_DIR"
mv "$TILE_TMP" "$OUTPUT_DIR"
rm -rf "$OLD_DIR"

echo "==> Done. Tiles at $OUTPUT_DIR"
echo ""
echo "Next steps:"
echo "  Dev:  tiles served at http://who-owns-atlanta.local/tiles/{z}/{x}/{y}.pbf"
echo "  Prod: aws s3 sync $OUTPUT_DIR s3://who-owns-atlanta-tiles/ --content-type application/x-protobuf --delete"
