#!/usr/bin/env bash
# Build parcel vector tiles for Who Owns Atlanta? (Optimized Single-Pass)
#
# Usage:
#   scripts/build_tiles.sh [--output-dir DIR]

set -euo pipefail
set -x

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
  psql_cmd -c "DROP TABLE IF EXISTS _tile_oe_map; DROP TABLE IF EXISTS _tile_export_base;" 2>/dev/null || true
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
# Step 1: Materialize everything into a single table
# ---------------------------------------------------------------------------
# This is the "expensive" part, but we only do it once.
# It handles the geometry grouping and the is_condo logic for both counties.

echo "==> Materializing tile export data (expensive grouping)..."
psql_cmd -c "
  DROP TABLE IF EXISTS _tile_oe_map;
  CREATE TABLE _tile_oe_map AS
    SELECT unnest(oe.parcel_ids) AS parcel_id, oe.county, oe.cluster_id,
           oc.parcel_count AS cluster_size
    FROM owner_entities oe
    JOIN ownership_clusters oc ON oc.cluster_id = oe.cluster_id;
  CREATE INDEX ON _tile_oe_map (parcel_id, county);

  DROP TABLE IF EXISTS _tile_export_base;
  CREATE TABLE _tile_export_base AS
  WITH source AS (
    SELECT
        p.geometry,
        p.parcel_id,
        p.county,
        p.is_corporate,
        p.is_institutional,
        p.site_address,
        p.owner_name,
        p.is_condo_potential,
        p.home_type
    FROM parcels_unified p
  )
  SELECT
      geometry,
      (ARRAY_AGG(parcel_id))[1] AS parcel_id,
      (ARRAY_AGG(county))[1]    AS county,
      (MAX(is_corporate::int) > 0)     AS is_corporate,
      (MAX(is_institutional::int) > 0) AS is_institutional,
      MAX(cluster_id)            AS cluster_id,
      MAX(cluster_size)          AS cluster_size,
      (ARRAY_AGG(site_address))[1] AS site_address,
      (ARRAY_AGG(owner_name))[1]   AS owner_name,
      COUNT(*)                   AS unit_count,
      (COUNT(*) > 1 OR ST_Area(geometry) < 2e-9 OR MAX(is_condo_potential) > 0) AS is_condo,
      MAX(home_type)             AS home_type
  FROM (
      SELECT
          s.geometry,
          s.parcel_id,
          s.county,
          s.is_corporate,
          s.is_institutional,
          s.is_condo_potential,
          s.home_type,
          m.cluster_id,
          m.cluster_size,
          s.site_address,
          s.owner_name
      FROM source s
      LEFT JOIN _tile_oe_map m
        ON m.parcel_id = s.parcel_id AND m.county = s.county
  ) sub
  GROUP BY geometry;

  CREATE INDEX ON _tile_export_base USING GIST (geometry);
  ANALYZE _tile_export_base;
"

# ---------------------------------------------------------------------------
# Step 2: Export to tiles in parallel
# ---------------------------------------------------------------------------

echo "==> Starting parallel tippecanoe passes..."

TILE_TMP_OVERVIEW="$WORK_DIR/tiles_overview"
TILE_TMP_LOW="$WORK_DIR/tiles_low"
TILE_TMP_HIGH="$WORK_DIR/tiles_high"

# Overview SQL — no cluster_id/cluster_size (not used at z10-12)
OVERVIEW_SQL="SELECT geometry, parcel_id, county, is_corporate, is_institutional, unit_count, is_condo, home_type FROM _tile_export_base ORDER BY ST_Area(geometry) DESC"

# Pass 1a: z10-11 (city overview — feature-dropped, capped at 1.5 MB per tile)
# tippecanoe's natural dropping keeps the largest parcels (by area), discarding
# sub-pixel ones first. This gives a representative color distribution without
# the 15 MB / 8.7 MB tiles that were timing out.
tippecanoe \
  --output-to-directory "$TILE_TMP_OVERVIEW" \
  --no-tile-compression \
  --minimum-zoom=10 \
  --maximum-zoom=11 \
  --layer=parcels \
  --attribute-type=is_corporate:bool \
  --attribute-type=is_institutional:bool \
  --attribute-type=is_condo:bool \
  --attribute-type=unit_count:int \
  --attribute-type=home_type:string \
  --simplification=10 \
  --maximum-tile-bytes=1500000 \
  --drop-smallest-as-needed \
  <(PGPASSWORD="$DB_PASS" ogr2ogr -f GeoJSON /vsistdout/ \
      "PG:host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USER password=$DB_PASS" \
      -sql "$OVERVIEW_SQL" \
      -nln parcels)

# Pass 1b: z12 (neighbourhood overview — full features, no cluster attrs)
tippecanoe \
  --output-to-directory "$TILE_TMP_LOW" \
  --no-tile-compression \
  --minimum-zoom=12 \
  --maximum-zoom=12 \
  --layer=parcels \
  --attribute-type=is_corporate:bool \
  --attribute-type=is_institutional:bool \
  --attribute-type=is_condo:bool \
  --attribute-type=unit_count:int \
  --attribute-type=home_type:string \
  --simplification=10 \
  --no-tile-size-limit \
  --no-feature-limit \
  <(PGPASSWORD="$DB_PASS" ogr2ogr -f GeoJSON /vsistdout/ \
      "PG:host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USER password=$DB_PASS" \
      -sql "$OVERVIEW_SQL" \
      -nln parcels)

# Pass 2: z13-14 (detail)
tippecanoe \
  --output-to-directory "$TILE_TMP_HIGH" \
  --no-tile-compression \
  --minimum-zoom=13 \
  --maximum-zoom=14 \
  --layer=parcels \
  --attribute-type=is_corporate:bool \
  --attribute-type=is_institutional:bool \
  --attribute-type=is_condo:bool \
  --attribute-type=unit_count:int \
  --attribute-type=home_type:string \
  --no-tile-size-limit \
  --no-feature-limit \
  <(PGPASSWORD="$DB_PASS" ogr2ogr -f GeoJSON /vsistdout/ \
      "PG:host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_USER password=$DB_PASS" \
      -sql "SELECT * FROM _tile_export_base ORDER BY ST_Area(geometry) DESC" \
      -nln parcels)

echo "    Exports complete."

# ---------------------------------------------------------------------------
# Step 3: Merge and Install
# ---------------------------------------------------------------------------

echo "==> Merging zoom ranges with tile-join..."
TILE_TMP="$WORK_DIR/tiles"
tile-join \
  --output-to-directory "$TILE_TMP" \
  --no-tile-compression \
  --no-tile-size-limit \
  "$TILE_TMP_OVERVIEW" "$TILE_TMP_LOW" "$TILE_TMP_HIGH"

echo "==> Installing tiles to $OUTPUT_DIR..."
OLD_DIR="${OUTPUT_DIR}.old"
rm -rf "$OLD_DIR"
[ -d "$OUTPUT_DIR" ] && mv "$OUTPUT_DIR" "$OLD_DIR"
mv "$TILE_TMP" "$OUTPUT_DIR"
rm -rf "$OLD_DIR"
chmod -R o+rX "$OUTPUT_DIR"

echo "==> Done. Tiles at $OUTPUT_DIR"
