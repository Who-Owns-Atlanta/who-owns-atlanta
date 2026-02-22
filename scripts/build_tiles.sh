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
# Steps 1 + 2: Export GeoJSON and build tiles — two-pass strategy
# ---------------------------------------------------------------------------
# Two tippecanoe passes on different zoom ranges, merged with tile-join:
#
#   z10–12 (overview): minimal attributes + --simplification=10 to keep tiles
#          small. cluster_size is unused here (overview opacity is flat 1.0).
#
#   z13–14 (detail): full attributes including site_address + owner_name for
#          hover tooltips. Default simplification (geometry accuracy matters
#          at this zoom for clicking and outlines).
#
# Both passes use --no-tile-size-limit --no-feature-limit to prevent the
# feature-dropping that caused gray-square artifacts previously.
#
# --no-tile-compression: raw .pbf — nginx/CloudFront handle Content-Encoding.
# Note: CloudFront auto-compression only applies to objects ≤ 10MB; large
# low-zoom tiles must be pre-gzipped before S3 upload if compression is needed.

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
    SELECT unnest(oe.parcel_ids) AS parcel_id, oe.county, oe.cluster_id,
           oc.parcel_count AS cluster_size
    FROM owner_entities oe
    JOIN ownership_clusters oc ON oc.cluster_id = oe.cluster_id;
  CREATE INDEX ON _tile_oe_map (parcel_id, county);
  ANALYZE _tile_oe_map;
"

echo "==> Pass 1/2: z10–12 overview tiles (minimal attributes, simplified geometry)..."

TILE_TMP_LOW="$WORK_DIR/tiles_low"

tippecanoe \
  --output-to-directory "$TILE_TMP_LOW" \
  --no-tile-compression \
  --minimum-zoom=10 \
  --maximum-zoom=12 \
  --layer=parcels \
  --attribute-type=is_corporate:bool \
  --attribute-type=is_institutional:bool \
  --simplification=10 \
  --no-tile-size-limit \
  --no-feature-limit \
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

LOW_COUNT=$(find "$TILE_TMP_LOW" -name "*.pbf" | wc -l)
LOW_SIZE=$(du -sh "$TILE_TMP_LOW" | cut -f1)
echo "    z10–12: $LOW_COUNT tiles ($LOW_SIZE)"

echo "==> Pass 2/2: z13–14 detail tiles (full attributes, tooltip data)..."

TILE_TMP_HIGH="$WORK_DIR/tiles_high"

tippecanoe \
  --output-to-directory "$TILE_TMP_HIGH" \
  --no-tile-compression \
  --minimum-zoom=13 \
  --maximum-zoom=14 \
  --layer=parcels \
  --attribute-type=is_corporate:bool \
  --attribute-type=is_institutional:bool \
  --no-tile-size-limit \
  --no-feature-limit \
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
            m.cluster_id,
            m.cluster_size,
            p.site_address,
            p.owner_name
        FROM parcels_unified p
        LEFT JOIN _tile_oe_map m
          ON m.parcel_id = p.parcel_id AND m.county = p.county
      " \
      -nln parcels \
      -lco COORDINATE_PRECISION=6)

HIGH_COUNT=$(find "$TILE_TMP_HIGH" -name "*.pbf" | wc -l)
HIGH_SIZE=$(du -sh "$TILE_TMP_HIGH" | cut -f1)
echo "    z13–14: $HIGH_COUNT tiles ($HIGH_SIZE)"

echo "==> Merging zoom ranges with tile-join..."

TILE_TMP="$WORK_DIR/tiles"

tile-join \
  --output-to-directory "$TILE_TMP" \
  --no-tile-compression \
  --no-tile-size-limit \
  "$TILE_TMP_LOW" "$TILE_TMP_HIGH"

TILE_COUNT=$(find "$TILE_TMP" -name "*.pbf" | wc -l)
TILE_SIZE=$(du -sh "$TILE_TMP" | cut -f1)
echo "    Merged: $TILE_COUNT tiles ($TILE_SIZE)"

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
