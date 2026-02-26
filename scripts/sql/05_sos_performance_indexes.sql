-- SOS performance indexes and precomputed tables
-- Run once after the sos schema is populated.
-- These are required for fast execution of scripts/10_sos_network_enrichment.py (Pass 2b).

-- 1. Functional index on sos.officers for normalized name lookups
--    Drops officer name lookup from ~6.6s (seq scan on 49M rows) to ~5ms.
CREATE INDEX CONCURRENTLY IF NOT EXISTS officers_upper_name_idx
    ON sos.officers (upper(trim(first_name)), upper(trim(last_name)));

-- 2. Precomputed global officer counts
--    Stores COUNT(DISTINCT control_number) per unique (fn, ln) across all 49M officer rows.
--    Used by script 10 to filter professional organizers/incorporators without a live scan.
--    Rebuild after major SOS data refreshes.
CREATE TABLE IF NOT EXISTS sos.officer_global_counts AS
SELECT upper(trim(first_name)) AS fn,
       upper(trim(last_name))  AS ln,
       COUNT(DISTINCT control_number) AS global_count
FROM sos.officers
WHERE trim(first_name) != '' AND trim(last_name) != ''
GROUP BY 1, 2;

CREATE UNIQUE INDEX IF NOT EXISTS officer_global_counts_fn_ln_idx
    ON sos.officer_global_counts (fn, ln);

ANALYZE sos.officer_global_counts;
