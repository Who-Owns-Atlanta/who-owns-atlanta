#!/bin/bash
# run_transformations.sh — Full data transformation pipeline
# Reruns everything after the initial parcel load.

set -e

# Ensure we are in the project root
cd "$(dirname "$0")/.."
ROOT_DIR=$(pwd)

LOG_FILE="data/pipeline_$(date +%Y%m%d_%H%M%S).log"

log() {
    echo "------------------------------------------------------------------------" | tee -a "$LOG_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
    echo "------------------------------------------------------------------------" | tee -a "$LOG_FILE"
}

log "Starting Who Owns Atlanta Transformation Pipeline..."
log "Note: High 'user' time relative to 'real' time indicates multi-core usage."

export PYTHONPATH=$PYTHONPATH:$ROOT_DIR/scripts

log "Step 02: Flagging corporate/institutional owners..."
{ time uv run scripts/02_flag_corporate_owners.py ; } >> "$LOG_FILE" 2>&1

# log "Step 03: Normalizing addresses (libpostal)..."
# { time uv run scripts/03_normalize_addresses.py ; } >> "$LOG_FILE" 2>&1

log "Step 04: Building ownership network and base clusters..."
{ time uv run scripts/04_ownership_network.py ; } >> "$LOG_FILE" 2>&1

log "Step 08: Matching GA SOS records..."
{ time uv run scripts/08_match_sos.py ; } >> "$LOG_FILE" 2>&1

log "Step 09: Enriching owners with SOS data..."
{ time uv run scripts/09_enrich_owners_sos.py ; } >> "$LOG_FILE" 2>&1

log "Step 10: Refining clusters via SOS network..."
{ time uv run scripts/10_sos_network_enrichment.py ; } >> "$LOG_FILE" 2>&1

log "Step 10b: Final cluster refinement (Fusion/Fission)..."
{ time uv run scripts/10b_cluster_refinement.py ; } >> "$LOG_FILE" 2>&1

log "Step 11: City enrichment (Neighborhoods/NPUs/Zoning)..."
{ time uv run scripts/11_city_enrichment.py ; } >> "$LOG_FILE" 2>&1

log "Step 13: Refreshing Materialized Views (Required for Demographics)..."
{ time PGPASSWORD=woa psql -h localhost -p 5434 -U woa -d who_owns_atl -f scripts/sql/04_create_materialized_views.sql ; } >> "$LOG_FILE" 2>&1

log "Step 12: Calculating portfolio demographics..."
{ time uv run scripts/12_portfolio_demographics.py ; } >> "$LOG_FILE" 2>&1

# Final refresh if demographics adds info used by MVs (it doesn't currently, 
# but mv_cluster_stats might want to include demographics in the future).
# For now, 13 then 12 is enough.

log "Pipeline complete! Log saved to $LOG_FILE"
