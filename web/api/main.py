import os
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Who Owns Atlanta API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
                ORDER BY fulladdr
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

            if county == "fulton":
                cur.execute("""
                    SELECT
                        'fulton'            AS county,
                        parcelid            AS parcel_id,
                        address             AS site_address,
                        owner               AS owner_name,
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
                        NULL::text          AS owner_name2,
                        NULL::numeric       AS appraised_value,
                        NULL::text          AS zoning,
                        NULL::text          AS historic_district,
                        NULL::text          AS overlay_district
                    FROM fulton_parcels
                    WHERE parcelid = %(pid)s
                """, {"pid": parcel_id})
            else:
                cur.execute("""
                    SELECT
                        'dekalb'                            AS county,
                        COALESCE(parcelid, lowparcelid)     AS parcel_id,
                        siteaddress                         AS site_address,
                        ownernme1                           AS owner_name,
                        is_corporate,
                        is_institutional,
                        landuse                             AS land_use,
                        classdscrp                          AS property_class,
                        NULL                                AS land_acres,
                        NULL                                AS living_units,
                        city_neighborhood                   AS neighborhood,
                        city_npu                            AS npu,
                        city_council                        AS council_district,
                        pstladdress                         AS owner_mail_addr1,
                        NULLIF(TRIM(
                            COALESCE(NULLIF(TRIM(pstlcity),  '') || ', ', '') ||
                            COALESCE(NULLIF(TRIM(pstlstate), ''), '')         ||
                            COALESCE(' ' || NULLIF(TRIM(pstlzip5), ''), '')
                        ), '')                              AS owner_mail_addr2,
                        NULL::text                          AS exemption_code,
                        NULLIF(TRIM(ownernme2), '')         AS owner_name2,
                        totapr1                             AS appraised_value,
                        NULLIF(TRIM(zoning), '')            AS zoning,
                        NULLIF(TRIM(histdesc), '')          AS historic_district,
                        NULLIF(TRIM(ovldesc), '')           AS overlay_district
                    FROM dekalb_parcels
                    WHERE parcelid = %(pid)s OR lowparcelid = %(pid)s
                    LIMIT 1
                """, {"pid": parcel_id})

            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Parcel not found")
            result = dict(row)

            # Owner cluster — only expose if the cluster has >1 parcel (otherwise no profile page exists).
            # Use @> (array contains) — GIN index on parcel_ids (idx_oe_parcel_ids_gin) is used.
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
