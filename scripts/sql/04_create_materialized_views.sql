-- Materialized views for Who Owns Atlanta web interface
-- Run once after pipeline; refresh after data updates per planning/06_production_runbook.md

-- ---------------------------------------------------------------------------
-- mv_address_search
-- Address_Point spatially joined to county parcel tables.
-- Queried by /api/search for typeahead. Trigram index enables fast ILIKE.
-- Join directly to underlying tables (not parcels_unified view) so the
-- planner can use gist indexes on each table.
-- ---------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS mv_address_search CASCADE;

CREATE MATERIALIZED VIEW mv_address_search AS
SELECT
    ap."FULLADDR"   AS fulladdr,
    ap."LAT"        AS lat,
    ap."LON"        AS lon,
    f.parcelid      AS parcel_id,
    'fulton'::text  AS county
FROM gis."Address_Point" ap
CROSS JOIN LATERAL (
    SELECT fp.parcelid
    FROM fulton_parcels fp
    WHERE ST_DWithin(ap.geometry, fp.geometry, 0.0001)
    ORDER BY 
        (CASE WHEN fp.addrunit IS NOT NULL AND fp.addrunit <> '' AND ap."FULLADDR" ILIKE '%' || fp.addrunit || '%' THEN 1 ELSE 0 END) DESC,
        ST_Distance(ap.geometry, fp.geometry) ASC
    LIMIT 1
) f
WHERE ap."FULLADDR" IS NOT NULL AND ap."FULLADDR" <> ''

UNION ALL

SELECT
    ap."FULLADDR"                              AS fulladdr,
    ap."LAT"                                   AS lat,
    ap."LON"                                   AS lon,
    d.parcel_id                                AS parcel_id,
    'dekalb'::text                             AS county
FROM gis."Address_Point" ap
CROSS JOIN LATERAL (
    SELECT COALESCE(dp.parcelid, dp.lowparcelid) as parcel_id
    FROM dekalb_parcels dp
    WHERE ST_DWithin(ap.geometry, dp.geometry, 0.0001)
    ORDER BY 
        (CASE 
            WHEN (dp.unit IS NOT NULL AND dp.unit <> '' AND ap."FULLADDR" ILIKE '%' || dp.unit || '%') 
              OR (dp.unit_no IS NOT NULL AND dp.unit_no <> '' AND ap."FULLADDR" ILIKE '%' || dp.unit_no || '%') 
            THEN 1 ELSE 0 END) DESC,
        ST_Distance(ap.geometry, dp.geometry) ASC
    LIMIT 1
) d
WHERE ap."FULLADDR" IS NOT NULL AND ap."FULLADDR" <> '';

CREATE INDEX idx_mv_address_search_trgm
    ON mv_address_search USING GIN (fulladdr gin_trgm_ops);

CREATE INDEX idx_mv_address_search_parcel
    ON mv_address_search (county, parcel_id);


-- ---------------------------------------------------------------------------
-- mv_parcel_permits
-- Per-parcel complaint counts from Accela via Tax_Parcel bridge.
-- Only covers parcels that appear in Tax_Parcel (city-area parcels, ~171K).
-- "open" = status not in the Resolved list from view_records_with_parcels.
-- ---------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS mv_parcel_permits CASCADE;

CREATE MATERIALIZED VIEW mv_parcel_permits AS
SELECT
    p.parcel_id,
    p.county,
    count(*)                                                        AS permit_count,
    count(*) FILTER (WHERE r.status NOT IN (
        'Closed', 'Complied', 'No Violation Found', 'Void',
        'Complied - Dismissed', 'Judgement-Complied', 'Court Complied',
        'Not Complied-Dismissed', 'Dismissed-Not Complied',
        'Closed - Final-UTGE', 'Potential Duplicate'
    ))                                                              AS open_count,
    max(r.last_action_date)                                         AS last_action_date
FROM application.records r
JOIN gis."Tax_Parcel" tp
    ON (r.raw_data #>> '{parcels,0,parcelNumber}') = tp."LOWPARCELID"
JOIN parcels_unified p
    ON tp."LOWPARCELID" = p.parcel_id
GROUP BY p.parcel_id, p.county;

CREATE INDEX idx_mv_parcel_permits
    ON mv_parcel_permits (parcel_id, county);


-- ---------------------------------------------------------------------------
-- mv_cluster_stats
-- Per-cluster aggregate stats. Depends on mv_parcel_permits.
-- Unnests owner_entities.parcel_ids for the join to parcels_unified.
--
-- NOTE: registered_agents and primary_foreign_state are aggregated from
-- owner_entities — they do NOT exist as columns in ownership_clusters.
-- ownership_clusters only carries: cluster_id, entity_count, parcel_count,
-- owner_names, owner_addresses, sos_entity_count, primary_sos_status.
--
-- IMPORTANT: This view must be (re)created after any run of the clustering
-- pipeline (scripts 10_sos_network_enrichment.py or 11_*). The pipeline
-- uses DROP TABLE ... CASCADE on ownership_clusters which cascades to
-- mv_cluster_stats (and mv_leaderboard). Re-run this file after the pipeline:
--
--   PGPASSWORD=woa psql -h localhost -p 5434 -U woa who_owns_atl \
--     -f scripts/sql/04_create_materialized_views.sql
-- ---------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS mv_cluster_stats CASCADE;

CREATE MATERIALIZED VIEW mv_cluster_stats AS
SELECT
    oc.cluster_id,
    oc.entity_count,
    oc.parcel_count,
    oc.owner_names,
    array_remove(array_agg(DISTINCT oe.sos_registered_agent)
        FILTER (WHERE oe.sos_registered_agent IS NOT NULL), NULL) AS registered_agents,
    mode() WITHIN GROUP (ORDER BY oe.sos_status)                  AS primary_sos_status,
    mode() WITHIN GROUP (ORDER BY oe.sos_foreign_state)           AS primary_foreign_state,
    round(sum(p.land_acres)::numeric, 2)                          AS total_land_acres,
    count(*) FILTER (WHERE p.is_corporate)                        AS corporate_parcel_count,
    count(*) FILTER (WHERE p.is_institutional)                    AS institutional_parcel_count,
    coalesce(sum(pp.permit_count), 0)                             AS total_permit_count,
    coalesce(sum(pp.open_count), 0)                               AS total_open_count
FROM ownership_clusters oc
JOIN owner_entities oe
    ON oe.cluster_id = oc.cluster_id
JOIN LATERAL unnest(oe.parcel_ids) AS pid ON true
JOIN parcels_unified p
    ON p.parcel_id = pid AND p.county = oe.county
LEFT JOIN mv_parcel_permits pp
    ON pp.parcel_id = p.parcel_id AND pp.county = p.county
GROUP BY
    oc.cluster_id, oc.entity_count, oc.parcel_count, oc.owner_names;

CREATE INDEX idx_mv_cluster_stats
    ON mv_cluster_stats (cluster_id);

CREATE INDEX idx_mv_cluster_stats_parcel_count
    ON mv_cluster_stats (parcel_count DESC);


-- ---------------------------------------------------------------------------
-- mv_leaderboard
-- Top 500 clusters by parcel count. Sourced from mv_cluster_stats.
-- ---------------------------------------------------------------------------

DROP MATERIALIZED VIEW IF EXISTS mv_leaderboard CASCADE;

CREATE MATERIALIZED VIEW mv_leaderboard AS
SELECT
    cluster_id,
    owner_names,
    parcel_count,
    total_land_acres,
    corporate_parcel_count,
    institutional_parcel_count,
    total_permit_count,
    total_open_count,
    primary_sos_status,
    primary_foreign_state
FROM mv_cluster_stats
ORDER BY parcel_count DESC
LIMIT 500;

CREATE INDEX idx_mv_leaderboard
    ON mv_leaderboard (cluster_id);
