#!/usr/bin/env python3
"""Build static HTML pages for owner profiles and the leaderboard.

Run after each pipeline update.

Usage:
  uv run scripts/build_static_pages.py [--output-dir /var/www/who-owns-atlanta] [--min-parcels 2]
  uv run scripts/build_static_pages.py --owner-only --cluster-ids 1954,120,30,2
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

# SOS statuses that get a warning indicator
SOS_WARN_STATUSES = {"Dissolved", "Admin. Dissolved", "Owes Annual Registration"}

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
        <nav class="owner-quicknav">
          {% if alt_names %}<a href="#aka">other names →</a>{% endif %}
          <a href="#parcels">parcels →</a>
          <a href="/?cluster={{ cluster_id }}">view on map →</a>
        </nav>
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

    {# ── County Tax Parcel section ── #}
    <p class="profile-section-label">COUNTY TAX PARCEL <span class="src-ref"><a href="/faq/#data-sources">*</a></span></p>
    <dl class="profile-dl">
      {% if county_fulton %}
      <dt>Fulton County</dt><dd>{{ county_fulton }} parcel{{ 's' if county_fulton != 1 else '' }}</dd>
      {% endif %}
      {% if county_dekalb %}
      <dt>DeKalb County</dt><dd>{{ county_dekalb }} parcel{{ 's' if county_dekalb != 1 else '' }}</dd>
      {% endif %}
      <dt>Acreage</dt><dd>{{ acres }} acres</dd>
      {% if permit_count > 0 %}
      <dt>Complaints</dt><dd>{{ permit_count }} total{% if open_count > 0 %}, {{ open_count }} open{% endif %}</dd>
      {% endif %}
      {% if owner_addresses %}
      <dt>Mailing address{{ 'es' if owner_addresses|length > 1 else '' }}{% if owner_addresses|length == 8 %} <span class="cap-note">(first 8)</span>{% endif %}</dt>
      <dd>
        <ul class="address-list">
          {% for addr in owner_addresses %}<li>{{ addr | e }}</li>{% endfor %}
        </ul>
      </dd>
      {% endif %}
    </dl>

    {# ── Georgia SOS section ── #}
    {% if sos_rows %}
    <details class="sos-details" open>
      <summary>Georgia SOS <span class="src-ref"><a href="/faq/#data-sources">*</a></span></summary>
      <dl>
        {% if sos_statuses %}
        <dt>Status</dt>
        <dd>
          {% for st in sos_statuses %}
          <span class="{{ 'sos-status-warn' if st in sos_warn_statuses else '' }}">{{ st | e }}</span>{% if not loop.last %}, {% endif %}
          {% endfor %}
        </dd>
        {% endif %}
        {% if sos_states %}
        <dt>Formed in</dt>
        <dd>{{ sos_states | join(', ') | e }}</dd>
        {% endif %}
        {% if sos_business_types %}
        <dt>Type</dt>
        <dd>{{ sos_business_types | join('; ') | e }}</dd>
        {% endif %}
        {% if sos_agents %}
        <dt>Registered agent{{ 's' if sos_agents|length > 1 else '' }}{% if sos_agents|length == 10 %} <span class="cap-note">(first 10)</span>{% endif %}</dt>
        <dd>
          <ul class="ra-list">
            {% for agent in sos_agents %}
            <li><span class="ra-name">{{ agent.name | e }}</span>{% if agent.address %} — {{ agent.address | e }}{% endif %}</li>
            {% endfor %}
          </ul>
        </dd>
        {% endif %}
      </dl>
    </details>
    {% endif %}

    {# ── Neighborhood breakdown ── #}
    {% if neighborhoods %}
    <p class="profile-section-label">NEIGHBORHOOD BREAKDOWN <span class="src-ref"><a href="/faq/#data-sources">*</a></span> <span class="cap-note">(top 5)</span></p>
    <ul class="neighborhood-list">
      {% for nbhd in neighborhoods %}
      <li>
        <span class="nbhd-name">{{ nbhd.name | e }}</span>
        <span class="nbhd-count">{{ nbhd.count }} parcel{{ 's' if nbhd.count != 1 else '' }}</span>
      </li>
      {% endfor %}
    </ul>
    {% endif %}

    {% if alt_names %}
    <h2 id="aka">Also known as</h2>
    <ul class="alt-name-list aka-list">
      {% for name in alt_names %}<li>{{ name | e }}</li>{% endfor %}
    </ul>
    {% endif %}

    <h2 id="parcels">Parcels ({{ parcel_count_raw }}){% if parcel_table_capped %} <span class="table-cap-note">— showing first 200</span>{% endif %}</h2>
    {% if parcel_table_capped %}
    <p class="table-cap-msg">Showing 200 of {{ parcel_count_raw }} parcels. Use the map for the full list.</p>
    {% endif %}
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

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
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

def render_owner(cluster_id, stats, parcels, county_breakdown, sos_data, neighborhoods):
    names = stats["owner_names"] or []
    primary_name = names[0] if names else f"Cluster {cluster_id}"
    alt_names = sorted(names[1:]) if len(names) > 1 else []

    # Owner addresses — cap at 8, skip empty
    raw_addrs = stats.get("owner_addresses") or []
    owner_addresses = [a for a in raw_addrs if a and a.strip()][:8]

    # County breakdown
    county_fulton = county_breakdown.get("fulton", 0)
    county_dekalb = county_breakdown.get("dekalb", 0)

    # SOS data
    sos_rows = sos_data.get("rows", [])
    sos_statuses = sos_data.get("statuses", [])
    sos_states = sos_data.get("states", [])
    sos_business_types = sos_data.get("business_types", [])
    sos_agents = sos_data.get("agents", [])

    # Parcel table cap
    parcel_count_raw = fmt_int(stats["parcel_count"])
    parcel_table_capped = len(parcels) > 200
    parcels_display = parcels[:200]

    env = Environment(loader=BaseLoader())
    tmpl = env.from_string(OWNER_TMPL)
    return tmpl.render(
        page_title=primary_name,
        meta_description=f"{primary_name} owns {parcel_count_raw} parcels in the Atlanta area.",
        cluster_id=cluster_id,
        primary_name=primary_name,
        alt_names=alt_names,
        is_corporate=bool(stats["corporate_parcel_count"]),
        is_institutional=bool(stats["institutional_parcel_count"]),
        parcel_count=fmt_int(stats["parcel_count"]),
        parcel_count_raw=parcel_count_raw,
        acres=fmt_acres(stats["total_land_acres"]),
        corporate_count=fmt_int(stats["corporate_parcel_count"]),
        permit_count=fmt_int(stats["total_permit_count"]),
        open_count=fmt_int(stats["total_open_count"]),
        county_fulton=county_fulton,
        county_dekalb=county_dekalb,
        owner_addresses=owner_addresses,
        sos_rows=sos_rows,
        sos_statuses=sos_statuses,
        sos_states=sos_states,
        sos_business_types=sos_business_types,
        sos_agents=sos_agents,
        sos_warn_statuses=SOS_WARN_STATUSES,
        neighborhoods=neighborhoods,
        parcels=parcels_display,
        parcel_table_capped=parcel_table_capped,
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
            SELECT cs.cluster_id, cs.owner_names, cs.registered_agents,
                   cs.primary_sos_status, cs.primary_foreign_state,
                   cs.parcel_count, cs.total_land_acres,
                   cs.corporate_parcel_count, cs.institutional_parcel_count,
                   cs.total_permit_count, cs.total_open_count,
                   oc.owner_addresses
            FROM mv_cluster_stats cs
            JOIN ownership_clusters oc USING (cluster_id)
            WHERE cs.cluster_id = ANY(%s)
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

def fetch_county_breakdown_batch(conn, cluster_ids):
    """Returns {cluster_id: {'fulton': N, 'dekalb': N}}"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cluster_id, county, SUM(count) AS parcel_count
            FROM owner_entities
            WHERE cluster_id = ANY(%s)
            GROUP BY cluster_id, county
        """, (cluster_ids,))
        result = defaultdict(dict)
        for row in cur.fetchall():
            cid, county, count = row
            result[cid][county] = int(count)
        return result

def fetch_sos_details_batch(conn, cluster_ids):
    """Returns {cluster_id: {rows, statuses, states, business_types, agents}}"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT cluster_id,
                   sos_status, sos_foreign_state, sos_business_type,
                   sos_registered_agent, sos_registered_agent_address,
                   COUNT(*) AS entity_count
            FROM owner_entities
            WHERE cluster_id = ANY(%s) AND sos_status IS NOT NULL
            GROUP BY cluster_id, sos_status, sos_foreign_state, sos_business_type,
                     sos_registered_agent, sos_registered_agent_address
            ORDER BY cluster_id, entity_count DESC
        """, (cluster_ids,))
        rows_by_cluster = defaultdict(list)
        for row in cur.fetchall():
            rows_by_cluster[row["cluster_id"]].append(dict(row))

    result = {}
    for cid, rows in rows_by_cluster.items():
        # Aggregate unique values, preserving count-order for statuses
        seen_statuses = {}
        seen_states = set()
        seen_types = set()
        seen_agents = {}  # (name_lower, addr_lower) -> {name, address}

        for r in rows:
            st = r["sos_status"]
            if st:
                seen_statuses[st] = seen_statuses.get(st, 0) + int(r["entity_count"])

            state = r["sos_foreign_state"]
            if state:
                seen_states.add(state)

            btype = r["sos_business_type"]
            if btype:
                seen_types.add(btype)

            ra_name = (r["sos_registered_agent"] or "").strip()
            ra_addr = (r["sos_registered_agent_address"] or "").strip()
            if ra_name and ra_name.upper() not in ("NONE", ""):
                key = (ra_name.lower(), ra_addr.lower())
                if key not in seen_agents:
                    seen_agents[key] = {"name": ra_name, "address": ra_addr}

        statuses = [s for s, _ in sorted(seen_statuses.items(), key=lambda x: -x[1])]
        agents = list(seen_agents.values())[:10]

        result[cid] = {
            "rows": rows,
            "statuses": statuses,
            "states": sorted(seen_states),
            "business_types": sorted(seen_types),
            "agents": agents,
        }
    return result

def fetch_neighborhood_concentration_batch(conn, cluster_ids):
    """Returns {cluster_id: [{name, count}, ...]} top 5 per cluster."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT oe.cluster_id,
                   COALESCE(fp.city_neighborhood, dp.city_neighborhood) AS neighborhood,
                   COUNT(*) AS parcel_count
            FROM owner_entities oe
            JOIN LATERAL unnest(oe.parcel_ids) AS pid ON true
            LEFT JOIN fulton_parcels fp ON fp.parcelid = pid AND oe.county = 'fulton'
            LEFT JOIN dekalb_parcels dp ON dp.parcelid = pid AND oe.county = 'dekalb'
            WHERE oe.cluster_id = ANY(%s)
              AND COALESCE(fp.city_neighborhood, dp.city_neighborhood) IS NOT NULL
            GROUP BY oe.cluster_id, COALESCE(fp.city_neighborhood, dp.city_neighborhood)
            ORDER BY oe.cluster_id, parcel_count DESC
        """, (cluster_ids,))
        by_cluster = defaultdict(list)
        for row in cur.fetchall():
            cid, nbhd, count = row
            by_cluster[cid].append({"name": nbhd, "count": int(count)})

    # Take top 5 per cluster
    return {cid: rows[:5] for cid, rows in by_cluster.items()}

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
            county_map = fetch_county_breakdown_batch(conn, batch)
            sos_map = fetch_sos_details_batch(conn, batch)
            nbhd_map = fetch_neighborhood_concentration_batch(conn, batch)

            for cid in batch:
                stats = stats_map.get(cid)
                if not stats:
                    continue
                parcels = parcels_map.get(cid, [])
                county_breakdown = county_map.get(cid, {})
                sos_data = sos_map.get(cid, {})
                neighborhoods = nbhd_map.get(cid, [])
                html = render_owner(cid, stats, parcels, county_breakdown, sos_data, neighborhoods)
                out_path = output_dir / "owner" / str(cid) / "index.html"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(html)
                written += 1
    finally:
        conn.close()

    return written


def build_owner_pages(conn, output_dir, min_parcels, num_workers, cluster_ids_override=None):
    if cluster_ids_override is not None:
        cluster_ids = cluster_ids_override
        print(f"Building {len(cluster_ids)} owner pages (from --cluster-ids) "
              f"across {num_workers} workers...")
    else:
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
    parser.add_argument("--cluster-ids", type=str, default=None,
                        help="Comma-separated cluster IDs to build (bypasses --min-parcels fetch)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    cluster_ids_override = None
    if args.cluster_ids:
        cluster_ids_override = [int(x.strip()) for x in args.cluster_ids.split(",")]

    conn = psycopg2.connect(DB_URL)
    try:
        if not args.owner_only:
            build_leaderboard(conn, output_dir)
        if not args.leaderboard_only:
            build_owner_pages(conn, output_dir, args.min_parcels, args.workers,
                              cluster_ids_override=cluster_ids_override)
    finally:
        conn.close()

    print("All done.")

if __name__ == "__main__":
    main()
