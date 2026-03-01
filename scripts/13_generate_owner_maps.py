"""Generate static map images for top property owners.

Uses shot-scraper to capture income and renter choropleth maps for owners with 100+ parcels.
"""

import os
import psycopg2
import subprocess
import time
import sys
from pathlib import Path

DB_URL = os.environ.get("DATABASE_URL", "postgresql://woa:woa@localhost:5434/who_owns_atl")
OUTPUT_DIR = Path("web/frontend/img/owners")
PORT = 8001

def fetch_top_owners(conn, min_parcels=100, limit=None):
    with conn.cursor() as cur:
        query = "SELECT cluster_id FROM mv_cluster_stats WHERE parcel_count >= %s ORDER BY parcel_count DESC"
        params = [min_parcels]
        if limit:
            query += " LIMIT %s"
            params.append(limit)
        cur.execute(query, params)
        return [row[0] for row in cur.fetchall()]

def generate_maps(limit=None):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    conn = psycopg2.connect(DB_URL)
    cluster_ids = fetch_top_owners(conn, limit=limit)
    conn.close()
    
    print(f"Generating maps for {len(cluster_ids)} owners...")
    
    # Start temporary web server
    server_proc = subprocess.Popen(
        ["python3", "-m", "http.server", str(PORT), "--directory", "web/frontend"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
    try:
        # Give server a second to start
        time.sleep(2)
        
        for cid in cluster_ids:
            for mode in ["income", "renter"]:
                out_file = OUTPUT_DIR / f"cluster_{cid}_{mode}.png"
                if out_file.exists():
                    continue
                
                url = f"http://localhost:{PORT}/owner_visual.html?cluster_id={cid}&mode={mode}"
                print(f"  Capturing cluster {cid} {mode} map...")
                
                # Use shot-scraper
                # --wait 3000 ensures map layers load
                try:
                    subprocess.run([
                        "shot-scraper", url, 
                        "-o", str(out_file), 
                        "--wait", "3000",
                        "--width", "800",
                        "--height", "600"
                    ], check=True)
                except Exception as e:
                    print(f"    Error capturing {cid} {mode}: {e}")
                    
    finally:
        server_proc.terminate()
        server_proc.wait()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    
    generate_maps(limit=args.limit)
    print("\nDone.")
