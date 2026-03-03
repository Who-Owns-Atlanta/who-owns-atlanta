"""Generate static map images for property owners using Playwright directly.

Provides better control over WebGL flags for headless rendering.
"""

import os
import asyncio
import psycopg2
import subprocess
import time
from pathlib import Path
from playwright.async_api import async_playwright

DB_URL = os.environ.get("DATABASE_URL", "postgresql://woa:woa@localhost:5434/who_owns_atl")
OUTPUT_DIR = Path("web/frontend/img/owners")
PORT = 8001

async def fetch_top_owners(min_parcels=10):
    conn = psycopg2.connect(DB_URL)
    with conn.cursor() as cur:
        query = "SELECT cluster_id FROM mv_cluster_stats WHERE atlanta_parcel_count >= %s ORDER BY atlanta_parcel_count DESC"
        cur.execute(query, (min_parcels,))
        ids = [row[0] for row in cur.fetchall()]
    conn.close()
    return ids

async def capture_map(browser_context, cluster_id, mode):
    url = f"http://localhost:{PORT}/owner_visual.html?cluster_id={cluster_id}&mode={mode}"
    out_file = OUTPUT_DIR / f"cluster_{cluster_id}_{mode}.png"
    
    page = await browser_context.new_page()
    try:
        # Navigate and wait for the custom signal
        await page.goto(url)
        # Wait up to 30s for the map to signal it is idle/rendered
        try:
            await page.wait_for_function("window.rendered === true", timeout=30000)
        except Exception as e:
            print(f"  Warning for cluster {cluster_id} {mode}: Timeout waiting for signal, capturing anyway.")
        
        # Additional small sleep to ensure labels/dots are crisp
        await asyncio.sleep(1)
        
        await page.screenshot(path=str(out_file), animations="disabled")
        size = out_file.stat().st_size
        print(f"  Captured cluster {cluster_id} {mode} ({size/1024:.1f} KB)")
        return size > 10000 # Return true if size looks reasonable
    except Exception as e:
        print(f"  Error capturing cluster {cluster_id} {mode}: {e}")
        return False
    finally:
        await page.close()

async def main(cluster_ids=None, workers=4):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    if cluster_ids:
        cids = cluster_ids
    else:
        cids = await fetch_top_owners()
        
    print(f"Generating maps for {len(cids)} owners using {workers} parallel tasks...")

    # Start temporary web server
    server_proc = subprocess.Popen(
        ["python3", "-m", "http.server", str(PORT), "--directory", "web/frontend"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    
    try:
        await asyncio.sleep(3) # Give server a moment
        
        async with async_playwright() as p:
            # Crucial: pass flags to enable WebGL in headless mode and prevent crashes
            browser = await p.chromium.launch(
                args=[
                    "--use-gl=angle", 
                    "--use-angle=swiftshader",
                    "--single-process",
                    "--no-sandbox",
                    "--disable-dev-shm-usage"
                ]
            )
            # Use a higher device scale factor for better quality
            context = await browser.new_context(viewport={"width": 800, "height": 600}, device_scale_factor=2)
            
            # Process in chunks to limit concurrency
            for i in range(0, len(cids), workers):
                chunk = cids[i:i + workers]
                tasks = []
                for cid in chunk:
                    tasks.append(capture_map(context, cid, "income"))
                    tasks.append(capture_map(context, cid, "renter"))
                await asyncio.gather(*tasks)
                
            await browser.close()
            
    finally:
        server_proc.terminate()
        server_proc.wait()
        
    # Ensure images are world-readable
    print("Setting permissions on generated images...")
    subprocess.run(["chmod", "-R", "644", str(OUTPUT_DIR)], check=False)
    subprocess.run(["chmod", "755", str(OUTPUT_DIR)], check=False)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster-ids", type=str, help="Comma-separated cluster IDs")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    
    c_ids = None
    if args.cluster_ids:
        c_ids = [int(x.strip()) for x in args.cluster_ids.split(",")]
        
    asyncio.run(main(cluster_ids=c_ids, workers=args.workers))
