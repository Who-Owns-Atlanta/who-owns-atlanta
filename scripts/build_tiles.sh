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
GEOJSON="$WORK_DIR/parcels.geojson"

trap 'rm -rf "$WORK_DIR"' EXIT

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
# Step 1: Export GeoJSON from PostGIS
# ---------------------------------------------------------------------------
# Join parcels_unified to owner_entities via unnest so each parcel gets
# its cluster_id. LEFT JOIN so parcels with no entity row are still exported.
# ogr2ogr is faster than psql for large geometry exports.

echo "==> Exporting GeoJSON from PostGIS..."

PGPASSWORD="$DB_PASS" ogr2ogr \
  -f GeoJSON "$GEOJSON" \
  "PG:host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USER password=$DB_PASS" \
  -sql "
    SELECT
        p.geometry,
        p.parcel_id,
        p.county,
        p.is_corporate::int  AS is_corporate,
        p.is_institutional::int AS is_institutional,
        oe.cluster_id
    FROM parcels_unified p
    LEFT JOIN LATERAL (
        SELECT cluster_id
        FROM owner_entities
        WHERE p.parcel_id = ANY(parcel_ids)
          AND county = p.county
        LIMIT 1
    ) oe ON true
  " \
  -nln parcels \
  -lco COORDINATE_PRECISION=6

FEATURE_COUNT=$(grep -c '"type":"Feature"' "$GEOJSON" || true)
echo "    Exported ~$FEATURE_COUNT features ($(du -sh "$GEOJSON" | cut -f1))"

# ---------------------------------------------------------------------------
# Step 2: Build tiles with tippecanoe
# ---------------------------------------------------------------------------
# Zoom 10–14:
#   z10–12: simplified overview — color by is_corporate/is_institutional
#   z13–14: full detail — color by cluster_id
#
# --coalesce-densest-as-needed: merge features at low zoom to stay under tile
#   size limits rather than dropping them entirely.
# --no-tile-compression: tiles served as raw .pbf — nginx/CloudFront handle
#   transport compression via Content-Encoding separately.

echo "==> Running tippecanoe..."

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
  "$GEOJSON"

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
