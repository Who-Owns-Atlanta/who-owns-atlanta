#!/usr/bin/env python3
"""Build static HTML pages for owner profiles and the leaderboard.

Run after each pipeline update.

Usage:
  uv run scripts/build_static_pages.py [--output-dir /var/www/who-owns-atlanta] [--min-parcels 2]
"""

import argparse
import os
import sys
import time
import multiprocessing
from collections import defaultdict
from pathlib import Path

import psycopg2
import psycopg2.extras
from jinja2 import Environment, BaseLoader

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_URL = os.environ.get("DATABASE_URL", "postgresql://woa:woa@localhost:5434/who_owns_atl")
BATCH_SIZE = 500

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_BASE_HEAD = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ page_title }} — Who Owns Atlanta?</title>
  <meta name="description" content="{{ meta_description }}">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.classless.min.css">
  <link rel="stylesheet" href="/css/style.css">
  <link rel="stylesheet" href="/css/content.css">
</head>
<body class="content-page">
  <header>
    <a href="/" class="site-name">Who Owns Atlanta?</a>
    <nav class="header-nav">
      <a href="/leaderboard/">Leaderboard</a>
    </nav>
  </header>
  <main class="content-main">
"""

_BASE_FOOT = """\
  </main>
  <footer>
    <nav>
      <a href="/leaderboard/">Leaderboard</a>
      <a href="/about/">About</a>
      <a href="/methodology/">Methodology</a>
      <a href="/faq/">FAQ</a>
    </nav>
  </footer>
</body>
</html>
"""

LEADERBOARD_TMPL = _BASE_HEAD + """\
    <h1>Top Landlords in Atlanta</h1>
    <p class="lead">Ranked by parcel count across Fulton and DeKalb counties.
      <span class="muted">{{ total }} owners shown.</span></p>

    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Owner</th>
          <th class="num">Parcels</th>
          <th class="num">Acres</th>
          <th>Flags</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td class="rank">{{ loop.index }}</td>
          <td class="owner-cell">
            <a href="/owner/{{ r.cluster_id }}/">{{ r.primary_name | e }}</a>
            {% if r.alt_names %}
            <div class="alt-names">{{ r.alt_names | e }}</div>
            {% endif %}
          </td>
          <td class="num">{{ r.parcel_count }}</td>
          <td class="num">{{ r.acres }}</td>
          <td class="flags-cell">
            {% if r.is_corporate %}<span class="badge-corporate">CORPORATE</span>{% endif %}
            {% if r.is_institutional %}<span class="badge-institutional">INSTITUTIONAL</span>{% endif %}
            {% if r.foreign_state and r.foreign_state != 'Georgia' %}
            <span class="badge-state">{{ r.foreign_state | e }}</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
""" + _BASE_FOOT

OWNER_TMPL = _BASE_HEAD + """\
    <div class="owner-header">
      <div class="owner-names">
        <h1>{{ primary_name | e }}</h1>
        {% if alt_names %}
        <ul class="alt-name-list">
          {% for name in alt_names %}<li>{{ name | e }}</li>{% endfor %}
        </ul>
        {% endif %}
      </div>
      <div class="owner-flags">
        {% if is_corporate %}<span class="badge-corporate">CORPORATE</span>{% endif %}
        {% if is_institutional %}<span class="badge-institutional">INSTITUTIONAL</span>{% endif %}
      </div>
    </div>

    <div class="stats-row">
      <div class="stat">
        <span class="stat-value">{{ parcel_count }}</span>
        <span class="stat-label">parcels</span>
      </div>
      <div class="stat">
        <span class="stat-value">{{ acres }}</span>
        <span class="stat-label">acres</span>
      </div>
      {% if corporate_count > 0 %}
      <div class="stat">
        <span class="stat-value">{{ corporate_count }}</span>
        <span class="stat-label">corporate</span>
      </div>
      {% endif %}
      {% if permit_count > 0 %}
      <div class="stat">
        <span class="stat-value">{{ permit_count }}</span>
        <span class="stat-label">complaints{% if open_count > 0 %} ({{ open_count }} open){% endif %}</span>
      </div>
      {% endif %}
    </div>

    {% if sos_status or registered_agents or foreign_state %}
    <details class="sos-details">
      <summary>Georgia SOS filing</summary>
      <dl>
        {% if sos_status %}<dt>Status</dt><dd>{{ sos_status | e }}</dd>{% endif %}
        {% if foreign_state %}<dt>State</dt><dd>{{ foreign_state | e }}</dd>{% endif %}
        {% if registered_agents %}
        <dt>Registered agent</dt>
        <dd>{{ registered_agents | join(', ') | e }}</dd>
        {% endif %}
      </dl>
    </details>
    {% endif %}

    <a href="/?cluster={{ cluster_id }}" class="map-link">View on map →</a>

    <h2>Parcels ({{ parcel_count }})</h2>
    <div class="table-scroll">
    <table class="parcel-table">
      <thead>
        <tr>
          <th>Address</th>
          <th>County</th>
          <th>Owner on record</th>
          <th>Flags</th>
        </tr>
      </thead>
      <tbody>
        {% for p in parcels %}
        <tr>
          <td>{{ p.site_address or p.parcel_id | e }}</td>
          <td class="county-cell">{{ p.county | title | e }}</td>
          <td class="owner-record">{{ p.owner_name or '' | e }}</td>
          <td class="flags-cell">
            {% if p.is_corporate %}<span class="badge-corporate">CORPORATE</span>{% endif %}
            {% if p.is_institutional %}<span class="badge-institutional">INSTITUTIONAL</span>{% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
""" + _BASE_FOOT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_acres(val):
    if val is None:
        return "—"
    return f"{float(val):,.1f}"

def fmt_int(val):
    if val is None:
        return 0
    return int(val)

def render_leaderboard(rows):
    env = Environment(loader=BaseLoader())
    tmpl = env.from_string(LEADERBOARD_TMPL)
    return tmpl.render(
        page_title="Top Landlords",
        meta_description="The top corporate and institutional property owners in Atlanta, ranked by parcel count across Fulton and DeKalb counties.",
        rows=rows,
        total=len(rows),
    )

def render_owner(cluster_id, stats, parcels):
    names = stats["owner_names"] or []
    primary_name = names[0] if names else f"Cluster {cluster_id}"
    alt_names = names[1:] if len(names) > 1 else []

    env = Environment(loader=BaseLoader())
    tmpl = env.from_string(OWNER_TMPL)
    return tmpl.render(
        page_title=primary_name,
        meta_description=f"{primary_name} owns {fmt_int(stats['parcel_count'])} parcels in the Atlanta area.",
        cluster_id=cluster_id,
        primary_name=primary_name,
        alt_names=alt_names,
        is_corporate=bool(stats["corporate_parcel_count"]),
        is_institutional=bool(stats["institutional_parcel_count"]),
        parcel_count=fmt_int(stats["parcel_count"]),
        acres=fmt_acres(stats["total_land_acres"]),
        corporate_count=fmt_int(stats["corporate_parcel_count"]),
        permit_count=fmt_int(stats["total_permit_count"]),
        open_count=fmt_int(stats["total_open_count"]),
        sos_status=stats["primary_sos_status"],
        foreign_state=stats["primary_foreign_state"],
        registered_agents=stats["registered_agents"] or [],
        parcels=parcels,
    )

# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def fetch_leaderboard(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT cluster_id, owner_names, parcel_count, total_land_acres,
                   corporate_parcel_count, institutional_parcel_count,
                   primary_sos_status, primary_foreign_state
            FROM mv_leaderboard
            ORDER BY parcel_count DESC
        """)
        return cur.fetchall()

def fetch_cluster_ids(conn, min_parcels):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cluster_id FROM mv_cluster_stats
            WHERE parcel_count >= %s
            ORDER BY cluster_id
        """, (min_parcels,))
        return [row[0] for row in cur.fetchall()]

def fetch_cluster_stats_batch(conn, cluster_ids):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT cluster_id, owner_names, registered_agents,
                   primary_sos_status, primary_foreign_state,
                   parcel_count, total_land_acres,
                   corporate_parcel_count, institutional_parcel_count,
                   total_permit_count, total_open_count
            FROM mv_cluster_stats
            WHERE cluster_id = ANY(%s)
        """, (cluster_ids,))
        return {row["cluster_id"]: dict(row) for row in cur.fetchall()}

def fetch_parcels_batch(conn, cluster_ids):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT oe.cluster_id,
                   p.parcel_id, p.county, p.site_address, p.owner_name,
                   p.is_corporate, p.is_institutional
            FROM owner_entities oe
            JOIN LATERAL unnest(oe.parcel_ids) AS pid ON true
            JOIN parcels_unified p ON p.parcel_id = pid AND p.county = oe.county
            WHERE oe.cluster_id = ANY(%s)
            ORDER BY oe.cluster_id, p.county, p.site_address
        """, (cluster_ids,))
        by_cluster = defaultdict(list)
        for row in cur.fetchall():
            by_cluster[row["cluster_id"]].append(dict(row))
        return by_cluster

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_leaderboard(conn, output_dir):
    print("Building leaderboard...", end=" ", flush=True)
    rows_raw = fetch_leaderboard(conn)

    rows = []
    for r in rows_raw:
        names = r["owner_names"] or []
        rows.append({
            "cluster_id": r["cluster_id"],
            "primary_name": names[0] if names else f"Cluster {r['cluster_id']}",
            "alt_names": ", ".join(names[1:4]) if len(names) > 1 else "",
            "parcel_count": fmt_int(r["parcel_count"]),
            "acres": fmt_acres(r["total_land_acres"]),
            "is_corporate": bool(r["corporate_parcel_count"]),
            "is_institutional": bool(r["institutional_parcel_count"]),
            "foreign_state": r["primary_foreign_state"],
        })

    out_path = output_dir / "leaderboard" / "index.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_leaderboard(rows))
    print(f"done ({len(rows)} rows → {out_path})")

def worker(args):
    """Worker function run in a subprocess. Processes a slice of cluster_ids."""
    cluster_ids, output_dir, db_url, worker_id = args
    output_dir = Path(output_dir)
    written = 0

    conn = psycopg2.connect(db_url)
    try:
        for i in range(0, len(cluster_ids), BATCH_SIZE):
            batch = cluster_ids[i:i + BATCH_SIZE]
            stats_map = fetch_cluster_stats_batch(conn, batch)
            parcels_map = fetch_parcels_batch(conn, batch)

            for cid in batch:
                stats = stats_map.get(cid)
                if not stats:
                    continue
                parcels = parcels_map.get(cid, [])
                html = render_owner(cid, stats, parcels)
                out_path = output_dir / "owner" / str(cid) / "index.html"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(html)
                written += 1
    finally:
        conn.close()

    return written


def build_owner_pages(conn, output_dir, min_parcels, num_workers):
    cluster_ids = fetch_cluster_ids(conn, min_parcels)
    total = len(cluster_ids)
    print(f"Building {total} owner pages (parcel_count >= {min_parcels}) "
          f"across {num_workers} workers...")

    # Split cluster_ids evenly across workers
    chunks = [cluster_ids[i::num_workers] for i in range(num_workers)]
    work_args = [(chunk, str(output_dir), DB_URL, i) for i, chunk in enumerate(chunks)]

    t0 = time.time()
    with multiprocessing.Pool(processes=num_workers) as pool:
        results = pool.map(worker, work_args)

    written = sum(results)
    elapsed = time.time() - t0
    print(f"done — {written} owner pages written in {elapsed:.1f}s "
          f"({written / elapsed:.0f} pages/sec)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build static HTML pages")
    parser.add_argument("--output-dir", default="/var/www/who-owns-atlanta",
                        help="Root output directory (default: /var/www/who-owns-atlanta)")
    parser.add_argument("--min-parcels", type=int, default=2,
                        help="Minimum parcel count to generate owner page (default: 2)")
    parser.add_argument("--workers", type=int, default=max(1, os.cpu_count() - 2),
                        help="Parallel worker processes (default: cpu_count - 2)")
    parser.add_argument("--leaderboard-only", action="store_true",
                        help="Only build the leaderboard page")
    parser.add_argument("--owner-only", action="store_true",
                        help="Only build owner profile pages")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    conn = psycopg2.connect(DB_URL)
    try:
        if not args.owner_only:
            build_leaderboard(conn, output_dir)
        if not args.leaderboard_only:
            build_owner_pages(conn, output_dir, args.min_parcels, args.workers)
    finally:
        conn.close()

    print("All done.")

if __name__ == "__main__":
    main()
