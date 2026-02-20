import os
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Who Owns Atlanta API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


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
def search(q: str = Query(..., min_length=3)):
    """Address autocomplete — top 8 matches from Address_Point."""
    # TODO: implement
    return {"results": []}


# ---------------------------------------------------------------------------
# Parcel detail
# ---------------------------------------------------------------------------

@app.get("/api/parcel/{county}/{parcel_id}")
def parcel(county: str, parcel_id: str):
    """Full parcel detail including owner, cluster, permits."""
    # TODO: implement
    return {}


# ---------------------------------------------------------------------------
# Owner cluster profile
# ---------------------------------------------------------------------------

@app.get("/api/owner/{cluster_id}")
def owner(cluster_id: int):
    """All parcels and stats for an ownership cluster."""
    # TODO: implement
    return {}


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

@app.get("/api/leaderboard")
def leaderboard():
    """Top clusters by parcel count (from mv_leaderboard materialized view)."""
    # TODO: implement
    return {"clusters": []}
