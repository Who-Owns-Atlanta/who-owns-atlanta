"""Generate static map images for top property owners (Parallelized).

Uses shot-scraper to capture income and renter choropleth maps for owners with 100+ parcels.
"""

import os
import psycopg2
import subprocess
import time
import sys
from pathlib import Path
from multiprocessing import Pool

DB_URL = os.environ.get("DATABASE_URL", "postgresql://woa:woa@localhost:5434/who_owns_atl")
OUTPUT_DIR = Path("web/frontend/img/owners")
PORT = 8001

def fetch_top_owners(conn, min_parcels=100, limit=None, cluster_ids=None):
    with conn.cursor() as cur:
        if cluster_ids:
            return cluster_ids
            
        query = "SELECT cluster_id FROM mv_cluster_stats WHERE parcel_count >= %s ORDER BY parcel_count DESC"
        params = [min_parcels]
        if limit:
            query += " LIMIT %s"
            params.append(limit)
        cur.execute(query, params)
        return [row[0] for row in cur.fetchall()]

def capture_task(args):
    """Worker function for a single map capture."""
    url, out_file, label = args
    
    try:
        subprocess.run([
            "shot-scraper", url, 
            "-o", str(out_file), 
            "--wait-for", "window.rendered === true",
            "--width", "800",
            "--height", "600"
        ], check=True, capture_output=True)
        return f"  Captured {label}"
    except Exception as e:
        return f"  Error capturing {label}: {e}"

def generate_maps(limit=None, workers=4, cluster_ids=None):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    conn = psycopg2.connect(DB_URL)
    cids = fetch_top_owners(conn, limit=limit, cluster_ids=cluster_ids)
    conn.close()
    
    print(f"Generating maps for {len(cids)} owners using {workers} workers...")
    
    # 1. Prepare tasks
    tasks = []
    for cid in cids:
        for mode in ["income", "renter"]:
            out_file = OUTPUT_DIR / f"cluster_{cid}_{mode}.png"
            url = f"http://localhost:{PORT}/owner_visual.html?cluster_id={cid}&mode={mode}"
            tasks.append((url, str(out_file), f"cluster {cid} {mode}"))

    # 2. Start temporary web server
    server_proc = subprocess.Popen(
        ["python3", "-m", "http.server", str(PORT), "--directory", "web/frontend"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
    try:
        # Give server a second to start
        time.sleep(2)
        
        # 3. Run tasks in parallel
        with Pool(processes=workers) as pool:
            for result in pool.imap_unordered(capture_task, tasks):
                print(result)
                    
    finally:
        server_proc.terminate()
        server_proc.wait()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--cluster-ids", type=str, help="Comma-separated list of cluster IDs")
    parser.add_argument("--workers", type=int, default=8, help="Number of parallel shot-scraper processes")
    args = parser.parse_args()
    
    c_ids = None
    if args.cluster_ids:
        c_ids = [int(x.strip()) for x in args.cluster_ids.split(",")]
    
    t0 = time.time()
    generate_maps(limit=args.limit, workers=args.workers, cluster_ids=c_ids)
    print(f"\nDone in {time.time()-t0:.1f}s")
