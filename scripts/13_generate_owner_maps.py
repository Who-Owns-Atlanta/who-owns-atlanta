"""Generate static map images for top property owners (Parallelized).

Uses shot-scraper to capture income and renter choropleth maps for owners with 10+ Atlanta parcels.
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

def fetch_top_owners(conn, min_parcels=10, limit=None, cluster_ids=None):
    with conn.cursor() as cur:
        if cluster_ids:
            return cluster_ids
            
        # Filter by atlanta_parcel_count to match demographic logic
        query = "SELECT cluster_id FROM mv_cluster_stats WHERE atlanta_parcel_count >= %s ORDER BY atlanta_parcel_count DESC"
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
        # Wait 10s for the map to finish loading and rendering
        result = subprocess.run([
            "shot-scraper", url, 
            "-o", str(out_file), 
            "--wait", "10000",
            "--width", "800",
            "--height", "600"
        ], check=True, capture_output=True, text=True)
        return f"  Captured {label}"
    except subprocess.CalledProcessError as e:
        return f"  Error capturing {label}: {e.stderr or e.stdout or str(e)}"
    except Exception as e:
        return f"  Error capturing {label}: {e}"

def generate_maps(limit=None, workers=4, cluster_ids=None):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    conn = psycopg2.connect(DB_URL)
    cids = fetch_top_owners(conn, limit=limit, cluster_ids=cluster_ids)
    conn.close()
    
    if not cids:
        print("No owners found matching criteria.")
        return

    print(f"Generating maps for {len(cids)} owners using {workers} workers...")
    
    # 1. Prepare tasks
    tasks = []
    for cid in cids:
        for mode in ["income", "renter"]:
            out_file = OUTPUT_DIR / f"cluster_{cid}_{mode}.png"
            url = f"http://localhost:{PORT}/owner_visual.html?cluster_id={cid}&mode={mode}"
            tasks.append((url, str(out_file), f"cluster {cid} {mode}"))

    # 2. Start temporary web server
    print(f"Starting web server on port {PORT}...")
    server_proc = subprocess.Popen(
        ["python3", "-m", "http.server", str(PORT), "--directory", "web/frontend"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
    try:
        # Wait for server to be responsive
        import requests
        retries = 10
        server_ready = False
        while retries > 0:
            try:
                r = requests.get(f"http://localhost:{PORT}/owner_visual.html", timeout=1)
                if r.status_code == 200:
                    server_ready = True
                    break
            except:
                pass
            time.sleep(1)
            retries -= 1
        
        if not server_ready:
            print("Error: Web server failed to start.")
            return

        print("Web server ready. Starting captures...")
        
        # 3. Run tasks in parallel
        with Pool(processes=workers) as pool:
            for result in pool.imap_unordered(capture_task, tasks):
                print(result)
                    
    finally:
        print(f"Stopping web server (PID {server_proc.pid})...")
        server_proc.terminate()
        server_proc.wait()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--cluster-ids", type=str, help="Comma-separated list of cluster IDs")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel shot-scraper processes")
    args = parser.parse_args()
    
    c_ids = None
    if args.cluster_ids:
        c_ids = [int(x.strip()) for x in args.cluster_ids.split(",")]
    
    t0 = time.time()
    generate_maps(limit=args.limit, workers=args.workers, cluster_ids=c_ids)
    print(f"\nDone in {time.time()-t0:.1f}s")
