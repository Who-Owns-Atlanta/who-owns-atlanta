import os
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Who Owns Atlanta API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://who-owns-atlanta.org",
        "http://who-owns-atlanta.local",
        "http://who-owns-atlanta.lan",
        "http://localhost",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Stable endpoints get a 24-hour public cache.
# Search results are query-specific and must not be cached.
# Set DEV_MODE=1 to disable caching entirely (useful during development).
_dev = os.environ.get("DEV_MODE", "").strip() == "1"
CACHE_1DAY = "no-store" if _dev else "public, max-age=86400"
NO_CACHE = "no-store"


def get_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Address search
# ---------------------------------------------------------------------------

@app.get("/api/search")
def search(response: Response, q: str = Query(..., min_length=3)):
    """Address autocomplete — top 8 matches from mv_address_search."""
    response.headers["Cache-Control"] = NO_CACHE
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (fulladdr)
                    fulladdr, lat, lon, parcel_id, county
                FROM mv_address_search
                WHERE fulladdr ILIKE %(q)s
                ORDER BY fulladdr, priority
                LIMIT 8
            """, {"q": q.upper() + "%"})
            return {"results": cur.fetchall()}


# ---------------------------------------------------------------------------
# Parcel detail
# ---------------------------------------------------------------------------

@app.get("/api/parcel/{county}/{parcel_id:path}")
def parcel(county: str, parcel_id: str, response: Response):
    """Full parcel detail including owner, cluster, and permit summary."""
    response.headers["Cache-Control"] = CACHE_1DAY
    county = county.lower()
    if county not in ("fulton", "dekalb"):
        raise HTTPException(status_code=400, detail="county must be fulton or dekalb")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # Optimized detail query using UNION ALL of direct table lookups to hit indexes
            cur.execute("""
                WITH target AS (
                    SELECT
                        parcelid, address, owner, owneraddr1, owneraddr2,
                        is_corporate, is_institutional, lucode, classcode,
                        landacres, livunits, city_neighborhood, city_npu, city_council,
                        excode, NULL::double precision AS appraised_value, NULL::text AS ownernme2,
                        NULL::text AS zoning, NULL::text AS histdesc, NULL::text AS ovldesc,
                        city_zoning,
                        CASE
                            WHEN city_zoning ~ '^R-[1-5]$|^R-[1-5][A-Z]$|^PD-H$' THEN 'Single-Family'
                            WHEN city_zoning ~ '^RG|^MR|^MRC|^C-|^I-|^SPI-|^PD-MU$' THEN 'Multi-Family / Other'
                            WHEN city_zoning IS NULL AND lucode IN ('101','107','110') THEN 'Single-Family'
                            WHEN city_zoning IS NULL AND lucode IN ('106','208','211','212','2A0','2A1','2A2') THEN 'Multi-Family / Condo'
                            ELSE 'Other'
                        END AS home_type,
                        geometry
                    FROM fulton_parcels
                    WHERE parcelid = %(pid)s AND %(county)s = 'fulton'

                    UNION ALL

                    SELECT
                        COALESCE(parcelid, lowparcelid), siteaddress, ownernme1, pstladdress,
                        NULLIF(TRIM(
                            COALESCE(NULLIF(TRIM(pstlcity),  '') || ', ', '') ||
                            COALESCE(NULLIF(TRIM(pstlstate), ''), '')         ||
                            COALESCE(' ' || NULLIF(TRIM(pstlzip5), ''), '')
                        ), ''),
                        is_corporate, is_institutional, landuse, classdscrp,
                        NULL::double precision, NULL::double precision, city_neighborhood, city_npu, city_council,
                        NULL::text, totapr1, ownernme2,
                        zoning, histdesc, ovldesc,
                        city_zoning,
                        CASE
                            WHEN city_zoning ~ '^R-[1-5]$|^R-[1-5][A-Z]$|^PD-H$' THEN 'Single-Family'
                            WHEN city_zoning ~ '^RG|^MR|^MRC|^C-|^I-|^SPI-|^PD-MU$' THEN 'Multi-Family / Other'
                            WHEN city_zoning IS NULL AND landuse IN ('SUB','TN','TC') THEN 'Single-Family'
                            WHEN city_zoning IS NULL AND landuse IN ('CRC','NC','RC') THEN 'Multi-Family / Other'
                            ELSE 'Other'
                        END AS home_type,
                        geometry
                    FROM dekalb_parcels
                    WHERE (parcelid = %(pid)s OR lowparcelid = %(pid)s) AND %(county)s = 'dekalb'
                    LIMIT 1
                )
                SELECT
                    %(county)s          AS county,
                    parcelid            AS parcel_id,
                    address             AS site_address,
                    owner               AS owner_name,
                    ownernme2           AS owner_name2,
                    is_corporate,
                    is_institutional,
                    lucode              AS land_use,
                    classcode           AS property_class,
                    landacres           AS land_acres,
                    livunits            AS living_units,
                    city_neighborhood   AS neighborhood,
                    city_npu            AS npu,
                    city_council        AS council_district,
                    owneraddr1          AS owner_mail_addr1,
                    owneraddr2          AS owner_mail_addr2,
                    excode              AS exemption_code,
                    appraised_value,
                    zoning,
                    histdesc            AS historic_district,
                    ovldesc             AS overlay_district,
                    city_zoning,
                    home_type,
                    ST_Y(ST_Centroid(geometry)) AS lat,
                    ST_X(ST_Centroid(geometry)) AS lon
                FROM target
            """, {"county": county, "pid": parcel_id})

            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Parcel not found")
            result = dict(row)

            # Owner cluster — hit GIN index on owner_entities(parcel_ids)
            cur.execute("""
                SELECT oe.cluster_id, oc.parcel_count, oe.sos_business_id
                FROM owner_entities oe
                JOIN ownership_clusters oc USING (cluster_id)
                WHERE oe.parcel_ids @> ARRAY[%(pid)s] AND oe.county = %(county)s
                LIMIT 1
            """, {"pid": parcel_id, "county": county})
            oe = cur.fetchone()
            has_profile = oe and oe["parcel_count"] >= 2
            result["cluster_id"]      = oe["cluster_id"]      if has_profile else None
            result["sos_business_id"] = oe["sos_business_id"] if has_profile else None

            # Related units (same building/location/development)
            # Optimized by splitting fulton/deKalb into indexed table lookups
            if county == 'fulton':
                cur.execute("""
                    WITH target AS (
                        SELECT parcelid, address, lucode, subdiv, geometry 
                        FROM fulton_parcels WHERE parcelid = %(pid)s
                    )
                    SELECT
                        p.parcelid      AS parcel_id,
                        p.address       AS site_address,
                        p.owner         AS owner_name,
                        p.is_corporate,
                        p.is_institutional
                    FROM fulton_parcels p, target
                    WHERE (
                        ST_Equals(p.geometry, target.geometry)
                        OR (
                            target.lucode IN ('106', '110')
                            AND p.lucode IN ('106', '107', '110', '111')
                            AND ST_DWithin(p.geometry, target.geometry, 0.001)
                            AND split_part(p.address, ' ', 2) = split_part(target.address, ' ', 2)
                        )
                        OR (
                            target.subdiv IS NOT NULL AND target.subdiv != ''
                            AND p.subdiv = target.subdiv
                        )
                        OR (
                            (target.lucode IN ('166', '188') OR (target.subdiv IS NOT NULL AND target.subdiv ILIKE '%%TOWNHOME%%'))
                            AND p.lucode IN ('106', '107', '110', '111')
                            AND ST_DWithin(p.geometry, target.geometry, 0.001)
                            AND (target.subdiv IS NULL OR target.subdiv = '' OR p.subdiv = target.subdiv)
                        )
                    )
                    AND p.parcelid != %(pid)s
                    ORDER BY p.address
                    LIMIT 500
                """, {"pid": parcel_id})
            else:
                cur.execute("""
                    WITH target AS (
                        SELECT parcelid, siteaddress, geometry 
                        FROM dekalb_parcels WHERE (parcelid = %(pid)s OR lowparcelid = %(pid)s) LIMIT 1
                    )
                    SELECT
                        COALESCE(p.parcelid, p.lowparcelid) AS parcel_id,
                        p.siteaddress AS site_address,
                        p.ownernme1   AS owner_name,
                        p.is_corporate,
                        p.is_institutional
                    FROM dekalb_parcels p, target
                    WHERE (
                        ST_Equals(p.geometry, target.geometry)
                        OR (ST_DWithin(p.geometry, target.geometry, 0.0001) AND p.siteaddress = target.siteaddress)
                    )
                    AND (p.parcelid != %(pid)s OR p.lowparcelid != %(pid)s)
                    LIMIT 200
                """, {"pid": parcel_id})
            result["related_units"] = cur.fetchall()

            # Permit summary
            cur.execute("""
                SELECT permit_count, open_count, last_action_date
                FROM mv_parcel_permits
                WHERE parcel_id = %(pid)s AND county = %(county)s
            """, {"pid": parcel_id, "county": county})
            pp = cur.fetchone()
            result["permit_count"] = pp["permit_count"] if pp else 0
            result["open_permits"] = pp["open_count"] if pp else 0
            result["last_permit_date"] = pp["last_action_date"] if pp else None

            return result


# ---------------------------------------------------------------------------
# Owner cluster profile
# ---------------------------------------------------------------------------

@app.get("/api/owner/{cluster_id}")
def owner(cluster_id: int, response: Response):
    """Stats and parcel list for an ownership cluster."""
    response.headers["Cache-Control"] = CACHE_1DAY
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # Cluster stats
            cur.execute("""
                SELECT
                    cs.cluster_id,
                    cs.entity_count,
                    cs.parcel_count,
                    cs.owner_names,
                    cs.registered_agents,
                    cs.primary_sos_status,
                    cs.primary_foreign_state,
                    cs.total_land_acres,
                    cs.corporate_parcel_count,
                    cs.institutional_parcel_count,
                    cs.total_permit_count,
                    cs.total_open_count
                FROM mv_cluster_stats cs
                WHERE cs.cluster_id = %(cid)s
            """, {"cid": cluster_id})
            stats = cur.fetchone()
            if not stats:
                raise HTTPException(status_code=404, detail="Cluster not found")
            result = dict(stats)

            # Officers from SOS (one query across all entities in cluster)
            cur.execute("""
                SELECT DISTINCT 
                    trim(concat_ws(' ', o.first_name, o.last_name, o.company_name)) AS name,
                    o.description AS title
                FROM owner_entities oe
                JOIN sos.officers o
                    ON o.control_number = oe.sos_control_number
                WHERE oe.cluster_id = %(cid)s
                  AND oe.sos_control_number IS NOT NULL
                ORDER BY title, name
            """, {"cid": cluster_id})
            result["officers"] = cur.fetchall()

            # Parcel list with centroid lat/lon.
            # Query underlying tables directly (not parcels_unified view) so the
            # btree indexes on parcelid are used instead of a 615K-row UNION ALL seq scan.
            # lowparcelid is always NULL in dekalb data, so the OR branch is dropped.
            cur.execute("""
                SELECT
                    fp.parcelid           AS parcel_id,
                    'fulton'              AS county,
                    fp.address            AS address,
                    fp.owner              AS owner,
                    fp.is_corporate,
                    fp.is_institutional,
                    fp.city_neighborhood  AS neighborhood,
                    ST_Y(ST_Centroid(fp.geometry)) AS lat,
                    ST_X(ST_Centroid(fp.geometry)) AS lon
                FROM owner_entities oe
                JOIN LATERAL unnest(oe.parcel_ids) AS pid ON true
                JOIN fulton_parcels fp ON fp.parcelid = pid
                WHERE oe.cluster_id = %(cid)s AND oe.county = 'fulton'

                UNION ALL

                SELECT
                    dp.parcelid           AS parcel_id,
                    'dekalb'              AS county,
                    dp.siteaddress        AS address,
                    dp.ownernme1          AS owner,
                    dp.is_corporate,
                    dp.is_institutional,
                    dp.city_neighborhood  AS neighborhood,
                    ST_Y(ST_Centroid(dp.geometry)) AS lat,
                    ST_X(ST_Centroid(dp.geometry)) AS lon
                FROM owner_entities oe
                JOIN LATERAL unnest(oe.parcel_ids) AS pid ON true
                JOIN dekalb_parcels dp ON dp.parcelid = pid
                WHERE oe.cluster_id = %(cid)s AND oe.county = 'dekalb'

                ORDER BY county, address
            """, {"cid": cluster_id})
            result["parcels"] = cur.fetchall()

            return result


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

@app.get("/api/leaderboard")
def leaderboard(response: Response):
    """Top 500 clusters by parcel count."""
    response.headers["Cache-Control"] = CACHE_1DAY
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
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
                FROM mv_leaderboard
                ORDER BY parcel_count DESC
            """)
            return {"clusters": cur.fetchall()}
