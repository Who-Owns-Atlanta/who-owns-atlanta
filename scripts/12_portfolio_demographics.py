"""Calculate demographic profile for each property owner portfolio (Optimized).

Aggregates neighborhood demographics (Income, Renter %, Race) for all parcels in a cluster.
"""

import os
import psycopg2
import psycopg2.extras
import json
import time

DB_URL = os.environ.get("DATABASE_URL", "postgresql://woa:woa@localhost:5434/who_owns_atl")

def setup_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_demographics (
                cluster_id INTEGER PRIMARY KEY,
                atlanta_parcel_count INTEGER,
                avg_neighborhood_income NUMERIC,
                avg_neighborhood_renter_pct NUMERIC,
                avg_neighborhood_white_pct NUMERIC,
                avg_neighborhood_black_pct NUMERIC,
                income_bucket_counts JSONB, -- {bucket_name: count}
                market_share_json JSONB,    -- {neighborhood: {parcels, share_of_rentals}}
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            ALTER TABLE portfolio_demographics
              ADD COLUMN IF NOT EXISTS avg_neighborhood_hispanic_pct NUMERIC,
              ADD COLUMN IF NOT EXISTS avg_neighborhood_asian_pct NUMERIC,
              ADD COLUMN IF NOT EXISTS avg_neighborhood_poverty_pct NUMERIC,
              ADD COLUMN IF NOT EXISTS avg_neighborhood_home_value NUMERIC,
              ADD COLUMN IF NOT EXISTS avg_neighborhood_vacant_pct NUMERIC,
              ADD COLUMN IF NOT EXISTS home_value_bucket_counts JSONB;
        """)
        conn.commit()

def calculate_demographics(conn):
    print("Calculating portfolio demographics in bulk...")
    t0 = time.time()
    
    with conn.cursor() as cur:
        # 1. Clear old data or just use ON CONFLICT
        # Using a single complex query to calculate everything at once for all clusters with 10+ parcels
        cur.execute("""
            WITH cluster_parcels AS (
                SELECT oe.cluster_id, p.city_neighborhood
                FROM owner_entities oe
                JOIN LATERAL unnest(oe.parcel_ids) AS pid ON true
                JOIN parcels_unified p ON p.parcel_id = pid AND p.county = oe.county
                WHERE oe.cluster_id IN (SELECT cluster_id FROM mv_cluster_stats WHERE parcel_count >= 10)
            ),
            neighborhood_stats AS (
                SELECT
                    cp.cluster_id,
                    COUNT(*) as atlanta_parcel_count,
                    AVG(d.median_household_income) as avg_income,
                    AVG(d.renter_occupied_pct) as avg_renter,
                    AVG(d.white_pct) as avg_white,
                    AVG(d.black_pct) as avg_black,
                    AVG(d.hispanic_pct) as avg_hispanic,
                    AVG(d.asian_pct) as avg_asian,
                    AVG(d.below_poverty_count::float / NULLIF(d.total_households, 0) * 100) as avg_poverty_pct,
                    AVG(d.median_home_value) as avg_home_value,
                    AVG(d.vacant_units_pct) as avg_vacant_pct
                FROM cluster_parcels cp
                JOIN gis.neighborhood_demographics d ON cp.city_neighborhood = d.neighborhood_name
                GROUP BY cp.cluster_id
            ),
            income_buckets AS (
                SELECT 
                    cp.cluster_id,
                    jsonb_object_agg(bucket, count) as buckets
                FROM (
                    SELECT 
                        cp.cluster_id,
                        CASE 
                            WHEN d.median_household_income < 40000 THEN 'Low'
                            WHEN d.median_household_income < 57000 THEN 'Low-Mid'
                            WHEN d.median_household_income < 84000 THEN 'Mid'
                            WHEN d.median_household_income < 136000 THEN 'Mid-High'
                            ELSE 'High'
                        END as bucket,
                        COUNT(*) as count
                    FROM cluster_parcels cp
                    JOIN gis.neighborhood_demographics d ON cp.city_neighborhood = d.neighborhood_name
                    GROUP BY cp.cluster_id, bucket
                ) cp
                GROUP BY cp.cluster_id
            ),
            market_shares AS (
                SELECT
                    cluster_id,
                    jsonb_object_agg(neighborhood_name, stats) as shares
                FROM (
                    SELECT
                        cp.cluster_id,
                        d.neighborhood_name,
                        jsonb_build_object(
                            'parcels', COUNT(*),
                            'rental_share', ROUND((COUNT(*)::numeric / NULLIF(d.renter_occupied_count, 0)) * 100, 2)
                        ) as stats
                    FROM cluster_parcels cp
                    JOIN gis.neighborhood_demographics d ON cp.city_neighborhood = d.neighborhood_name
                    WHERE d.renter_occupied_count > 0
                    GROUP BY cp.cluster_id, d.neighborhood_name, d.renter_occupied_count
                ) sub
                GROUP BY cluster_id
            ),
            home_value_buckets AS (
                SELECT sub.cluster_id,
                       jsonb_object_agg(sub.bucket, sub.count) AS buckets
                FROM (
                    SELECT cp.cluster_id,
                           CASE
                               WHEN d.median_home_value < 150000  THEN '<$150k'
                               WHEN d.median_home_value < 300000  THEN '$150-300k'
                               WHEN d.median_home_value < 500000  THEN '$300-500k'
                               ELSE '$500k+'
                           END AS bucket,
                           COUNT(*) AS count
                    FROM cluster_parcels cp
                    JOIN gis.neighborhood_demographics d ON cp.city_neighborhood = d.neighborhood_name
                    WHERE d.median_home_value IS NOT NULL
                    GROUP BY cp.cluster_id, bucket
                ) sub
                GROUP BY sub.cluster_id
            )
            INSERT INTO portfolio_demographics (
                cluster_id, atlanta_parcel_count, avg_neighborhood_income, avg_neighborhood_renter_pct,
                avg_neighborhood_white_pct, avg_neighborhood_black_pct,
                income_bucket_counts, market_share_json,
                avg_neighborhood_hispanic_pct, avg_neighborhood_asian_pct,
                avg_neighborhood_poverty_pct, avg_neighborhood_home_value,
                avg_neighborhood_vacant_pct, home_value_bucket_counts
            )
            SELECT
                ns.cluster_id, ns.atlanta_parcel_count, ns.avg_income, ns.avg_renter, ns.avg_white, ns.avg_black,
                ib.buckets, ms.shares,
                ns.avg_hispanic, ns.avg_asian,
                ns.avg_poverty_pct, ns.avg_home_value,
                ns.avg_vacant_pct, hvb.buckets
            FROM neighborhood_stats ns
            LEFT JOIN income_buckets ib ON ns.cluster_id = ib.cluster_id
            LEFT JOIN market_shares ms ON ns.cluster_id = ms.cluster_id
            LEFT JOIN home_value_buckets hvb ON ns.cluster_id = hvb.cluster_id
            ON CONFLICT (cluster_id) DO UPDATE SET
                atlanta_parcel_count = EXCLUDED.atlanta_parcel_count,
                avg_neighborhood_income = EXCLUDED.avg_neighborhood_income,
                avg_neighborhood_renter_pct = EXCLUDED.avg_neighborhood_renter_pct,
                avg_neighborhood_white_pct = EXCLUDED.avg_neighborhood_white_pct,
                avg_neighborhood_black_pct = EXCLUDED.avg_neighborhood_black_pct,
                income_bucket_counts = EXCLUDED.income_bucket_counts,
                market_share_json = EXCLUDED.market_share_json,
                avg_neighborhood_hispanic_pct = EXCLUDED.avg_neighborhood_hispanic_pct,
                avg_neighborhood_asian_pct = EXCLUDED.avg_neighborhood_asian_pct,
                avg_neighborhood_poverty_pct = EXCLUDED.avg_neighborhood_poverty_pct,
                avg_neighborhood_home_value = EXCLUDED.avg_neighborhood_home_value,
                avg_neighborhood_vacant_pct = EXCLUDED.avg_neighborhood_vacant_pct,
                home_value_bucket_counts = EXCLUDED.home_value_bucket_counts,
                last_updated = CURRENT_TIMESTAMP;
        """)
        conn.commit()
    
    print(f"Done in {time.time()-t0:.1f}s")

if __name__ == "__main__":
    conn = psycopg2.connect(DB_URL)
    setup_table(conn)
    calculate_demographics(conn)
    conn.close()
