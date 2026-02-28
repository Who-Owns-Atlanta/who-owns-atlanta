#!/usr/bin/env python3
"""Build static HTML pages for owner profiles and the leaderboard.

Run after each pipeline update.

Usage:
  uv run scripts/build_static_pages.py [--output-dir /var/www/who-owns-atlanta] [--min-parcels 2]
  uv run scripts/build_static_pages.py --owner-only --cluster-ids 1954,120,30,2
"""

import argparse
import os
import re
import sys
import time
import multiprocessing
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote_plus

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

# Commercial RA firms — linkage to these is not meaningful
COMMERCIAL_RA_PATTERNS = [
    "%ct corporation%", "%c t corporation%",
    "%corporation service%", "%csc of%",
    "%registered agents inc%", "%northwest registered%",
    "%national registered%", "%cogency%",
    "%incorp services%", "%vcorp%", "%paracorp%",
    "%united states corporation%", "%corporate creations%",
    "%bcs corporate%", "%access management%",
    "%georgia registered agent%", "%homeowner management%",
    "%business filings%", "%capitol corporate%",
    "%republic registered%", "%registered agent solutions%",
    "%georgiagent%", "%anderson registered%",
    "%legalzoom%", "%registered agent group%",
    "%harbor compliance%", "%wolters kluwer%",
    "%agent solutions%",
]

def is_commercial_ra(name):
    if not name:
        return False
    name_lower = name.lower()
    for pat in COMMERCIAL_RA_PATTERNS:
        core = pat.strip('%')
        if core in name_lower:
            return True
    return False

# ---------------------------------------------------------------------------
# Jinja2 environment factory
# ---------------------------------------------------------------------------

def _make_env():
    """Create a Jinja2 Environment with our custom filters."""
    env = Environment(loader=BaseLoader())
    env.filters['urlencode'] = lambda s: quote_plus(str(s)) if s else ''
    return env

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_BASE_HEAD = """\
<!DOCTYPE html>
<html lang="en" data-theme="light">
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
      <a href="/l/">Leaderboards</a>
    </nav>
  </header>
  <main class="content-main">
"""

_BASE_FOOT = """\
  </main>
  <footer>
    <nav>
      <a href="/">Map</a>
      <a href="/l/">Leaderboards</a>
      <a href="/about/">About</a>
      <a href="/methodology/">Methodology</a>
      <a href="/faq/">FAQ</a>
    </nav>
  </footer>
</body>
</html>
"""

LEADERBOARD_TMPL = _BASE_HEAD + """\
    <h1>Leaderboards</h1>
    <nav class="leaderboard-subnav">
      <div class="subnav-group">
        <span class="subnav-label">Overall</span>
        <span class="subnav-current">Global</span>
        <a href="/l/agents/">Registered Agents</a>
        <a href="/l/addresses/">Shared Addresses</a>
      </div>
      <div class="subnav-group">
        <span class="subnav-label">County</span>
        <a href="/l/fulton/">Fulton</a>
        <a href="/l/dekalb/">DeKalb</a>
      </div>
      <div class="subnav-group">
        <span class="subnav-label">Atlanta</span>
        <a href="/l/atlanta/council/">Council Districts</a>
        <a href="/l/atlanta/npu/">NPUs</a>
        <a href="/l/atlanta/neighborhood/">Neighborhoods</a>
      </div>
    </nav>

    <h2>Global — Top Landlords in Atlanta</h2>
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
          <th class="num">Connected</th>
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
          <td class="num">{% if r.connection_count > 0 %}<a href="/owner/{{ r.cluster_id }}/#related" class="connection-count">{{ r.connection_count }}</a>{% endif %}</td>
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
          {% if alt_names %}<a href="#aka">names on record →</a>{% endif %}
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
      <dt>Mailing address{{ 'es' if owner_addresses|length > 1 else '' }} ({{ owner_addresses|length }})</dt>
      <dd>
        <div class="scroll-box">
        <ul class="address-list">
          {% for addr in owner_addresses %}<li>{{ addr | e }}</li>{% endfor %}
        </ul>
        </div>
      </dd>
      {% endif %}
    </dl>

    {# ── Georgia SOS section (flat — no expand/hide) ── #}
    {% if sos_rows %}
    <p class="profile-section-label">GEORGIA SOS <span class="src-ref"><a href="/faq/#data-sources">*</a></span></p>
    <dl class="profile-dl">
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
      {% if principal_offices %}
      <dt>Principal office</dt>
      <dd>
        {% for po in principal_offices %}
        {% if loop.index > 1 %} · {% endif %}
        {% if po.out_of_state %}
        <span class="badge-state">{{ po.display | e }}</span>
        {% else %}
        {{ po.display | e }}
        {% endif %}
        {% endfor %}
      </dd>
      {% endif %}
      {% if sos_agents %}
      <dt>Registered agent{{ 's' if sos_agents|length > 1 else '' }} ({{ sos_agents|length }})</dt>
      <dd>
        <div class="scroll-box">
        <ul class="ra-list">
          {% for agent in sos_agents %}
          <li>
            {% if agent.ra_id and agent.ra_id in linkable_agent_ids %}
            <a href="/agent/{{ agent.ra_id }}/" class="ra-name">{{ agent.name | e }}</a>
            {% else %}
            <span class="ra-name">{{ agent.name | e }}</span>
            {% endif %}
            {% if agent.address %} — {{ agent.address | e }}{% endif %}
          </li>
          {% endfor %}
        </ul>
        </div>
      </dd>
      {% endif %}
    </dl>
    {% endif %}

    {# ── Officers / Principals section ── #}
    {% if officers %}
    <p class="profile-section-label">OFFICERS / PRINCIPALS <span class="src-ref"><a href="/faq/#data-sources">*</a></span></p>
    <div class="scroll-box officer-box">
    <ul class="officer-list">
      {% for o in officers %}
      <li>
        {% if o.role %}<span class="officer-role">{{ o.role | e }}</span>{% endif %}
        <span class="officer-name">{{ o.name | e }}</span>
        {% if o.city or o.state %}
        <span class="officer-loc">— {{ o.city | e }}{% if o.city and o.state %}, {% endif %}{{ o.state | e }}</span>
        {% endif %}
      </li>
      {% endfor %}
    </ul>
    </div>
    {% endif %}

    {# ── Neighborhood breakdown ── #}
    {% if neighborhoods %}
    <p class="profile-section-label">NEIGHBORHOOD BREAKDOWN <span class="src-ref"><a href="/faq/#data-sources">*</a></span></p>
    <div class="neighborhood-scroll">
    <ul class="neighborhood-list">
      {% for nbhd in neighborhoods %}
      <li>
        <span class="nbhd-name">{{ nbhd.name | e }}</span>
        <span class="nbhd-count">{{ nbhd.count }} parcel{{ 's' if nbhd.count != 1 else '' }}</span>
        <a href="/?cluster={{ cluster_id }}&geo=neighborhood&area={{ nbhd.name_enc }}" class="nbhd-map-link" title="View on map">map →</a>
      </li>
      {% endfor %}
    </ul>
    </div>
    {% endif %}

    {# ── Related owners ── #}
    {% if related_owners %}
    <h2 id="related">Related owners</h2>
    <p class="related-subhead">Connected via shared registered agent or mailing address.</p>
    <div class="table-scroll">
    <table class="related-table">
      <thead>
        <tr>
          <th>Owner</th>
          <th>Via</th>
          <th class="num">Parcels</th>
        </tr>
      </thead>
      <tbody>
        {% for r in related_owners %}
        <tr>
          <td>
            {% if r.parcel_count >= 2 %}
            <a href="/owner/{{ r.cluster_id }}/">{{ r.primary_name | e }}</a>
            {% else %}
            {{ r.primary_name | e }}
            {% endif %}
          </td>
          <td class="connection-via">
            {% for item in r.via_items %}
            {% if item.url %}<a href="{{ item.url }}">{{ item.text | e }}</a>{% else %}{{ item.text | e }}{% endif %}
            {% if not loop.last %}, {% endif %}
            {% endfor %}
          </td>
          <td class="num">{{ r.parcel_count }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    {% if related_owners|length == 15 %}<p class="cap-note">Showing top 15 related owners.</p>{% endif %}
    {% endif %}

    {# ── Owner Names on record ── #}
    {% if alt_names %}
    <h2 id="aka">Owner Names on record</h2>
    <ul class="alt-name-list aka-list">
      {% for item in alt_names %}
      <li>
        {% if item.sos_business_id %}
        <a href="https://ecorp.sos.ga.gov/BusinessSearch/BusinessInformation?businessId={{ item.sos_business_id | e }}" target="_blank" rel="noopener">{{ item.name | e }}</a>
        {% else %}
        {{ item.name | e }}
        {% endif %}
      </li>
      {% endfor %}
    </ul>
    {% endif %}

    <h2 id="parcels">Parcels ({{ parcel_count_raw }}){% if parcel_table_capped %} <span class="table-cap-note">— showing first 200</span>{% endif %}</h2>
    {% if parcel_table_capped %}
    <p class="table-cap-msg">Showing 200 of {{ parcel_count_raw }} parcels. <a href="/?cluster={{ cluster_id }}">View all on map →</a></p>
    {% endif %}
    <div class="table-scroll">
    <table class="parcel-table">
      <thead>
        <tr>
          <th>Address</th>
          <th>County</th>
          <th>Owner on record</th>
          <th>Flags</th>
          <th></th>
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
          <td class="map-link-cell"><a href="/?parcel={{ p.county }}/{{ p.parcel_id | urlencode }}" title="View on map" class="map-link-small">map →</a></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
""" + _BASE_FOOT

AGENTS_INDEX_TMPL = _BASE_HEAD + """\
    <p class="breadcrumb"><a href="/l/">← Leaderboards</a></p>
    <h1>Registered Agents</h1>
    <p class="lead">Individual registered agents appearing across multiple owner clusters.
      <span class="muted">{{ total }} agents shown.</span></p>

    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Agent</th>
          <th class="num">Clusters</th>
          <th class="num">Parcels</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td><a href="/agent/{{ r.ra_id }}/">{{ r.name | e }}</a></td>
          <td class="num">{{ r.cluster_count }}</td>
          <td class="num">{{ r.total_parcels }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
""" + _BASE_FOOT

AGENT_TMPL = _BASE_HEAD + """\
    <p class="breadcrumb"><a href="/l/">← Leaderboards</a> / <a href="/l/agents/">Registered Agents</a></p>
    <div class="owner-header">
      <div class="owner-names">
        <h1>{{ agent_name | e }}</h1>
      </div>
    </div>

    <p class="lead">Registered agent for {{ cluster_count }} owner cluster{{ 's' if cluster_count != 1 else '' }}
      ({{ total_parcels }} parcel{{ 's' if total_parcels != 1 else '' }} total)</p>

    <div class="table-scroll">
    <table class="parcel-table">
      <thead>
        <tr>
          <th>Owner</th>
          <th class="num">Parcels</th>
          <th>Flags</th>
        </tr>
      </thead>
      <tbody>
        {% for row in clusters %}
        <tr>
          <td><a href="/owner/{{ row.cluster_id }}/">{{ row.primary_name | e }}</a></td>
          <td class="num">{{ row.parcel_count }}</td>
          <td class="flags-cell">
            {% if row.is_corporate %}<span class="badge-corporate">CORPORATE</span>{% endif %}
            {% if row.is_institutional %}<span class="badge-institutional">INSTITUTIONAL</span>{% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
""" + _BASE_FOOT

GEO_INDEX_TMPL = _BASE_HEAD + """\
    <p class="breadcrumb"><a href="/l/">← Leaderboards</a></p>
    <h1>{{ index_title }}</h1>
    <p class="lead">{{ index_lead }}
      <span class="muted">{{ total }} areas.</span></p>

    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>{{ area_label }}</th>
          <th class="num">Parcels</th>
          <th>Top owner</th>
          {% if rows and rows[0].map_url %}<th></th>{% endif %}
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td><a href="{{ r.url }}">{{ r.area | e }}</a></td>
          <td class="num">{{ r.total_parcels }}</td>
          <td class="muted">{{ r.top_owner | e }}</td>
          {% if r.map_url %}<td class="map-link-cell"><a href="{{ r.map_url }}" title="View on map" class="map-link-small">map →</a></td>{% endif %}
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
""" + _BASE_FOOT

GEO_LEADERBOARD_TMPL = _BASE_HEAD + """\
    <p class="breadcrumb"><a href="{{ index_url }}">← {{ index_label }}</a></p>
    <div class="geo-title-row">
      <h1>{{ area_name | e }}</h1>
      {% if area_map_url %}<a href="{{ area_map_url }}" class="geo-map-link">view on map →</a>{% endif %}
    </div>
    <p class="lead">Top property owners within this {{ geo_type_label }}.
      <span class="muted">{{ total }} owners shown, {{ area_total_parcels }} total parcels.</span></p>

    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Owner</th>
          <th class="num">In area</th>
          <th class="num">Total</th>
          <th>Flags</th>
          <th class="num">Connected</th>
          {% if geo_key %}<th></th>{% endif %}
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
          <td class="num">{{ r.local_parcel_count }}</td>
          <td class="num muted">{{ r.total_parcel_count }}</td>
          <td class="flags-cell">
            {% if r.is_corporate %}<span class="badge-corporate">CORPORATE</span>{% endif %}
            {% if r.is_institutional %}<span class="badge-institutional">INSTITUTIONAL</span>{% endif %}
            {% if r.foreign_state and r.foreign_state != 'Georgia' %}
            <span class="badge-state">{{ r.foreign_state | e }}</span>
            {% endif %}
          </td>
          <td class="num">{% if r.connection_count > 0 %}<a href="/owner/{{ r.cluster_id }}/#related" class="connection-count">{{ r.connection_count }}</a>{% endif %}</td>
          {% if geo_key %}
          <td class="map-link-cell"><a href="/?cluster={{ r.cluster_id }}&geo={{ geo_key }}&area={{ area_raw_enc }}" title="View on map" class="map-link">map →</a></td>
          {% endif %}
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
""" + _BASE_FOOT

ADDRESS_INDEX_TMPL = _BASE_HEAD + """\
    <p class="breadcrumb"><a href="/l/">← Leaderboards</a></p>
    <h1>Shared Mailing Addresses</h1>
    <p class="lead">Street addresses shared by multiple distinct owner clusters — a key signal
      for identifying networked ownership. <span class="muted">{{ total }} addresses shown.</span></p>

    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Address</th>
          <th class="num">Clusters</th>
          <th class="num">Parcels</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td><a href="/l/addresses/{{ r.slug }}/">{{ r.address | e }}</a></td>
          <td class="num">{{ r.cluster_count }}</td>
          <td class="num">{{ r.total_parcels }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
""" + _BASE_FOOT

ADDRESS_TMPL = _BASE_HEAD + """\
    <p class="breadcrumb"><a href="/l/">← Leaderboards</a> / <a href="/l/addresses/">Shared Addresses</a></p>
    <h1>{{ address | e }}</h1>
    <p class="lead">{{ cluster_count }} owner cluster{{ 's' if cluster_count != 1 else '' }}
      share this mailing address ({{ total_parcels }} total parcel{{ 's' if total_parcels != 1 else '' }}).</p>

    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Owner</th>
          <th class="num">Parcels</th>
          <th>Flags</th>
        </tr>
      </thead>
      <tbody>
        {% for row in clusters %}
        <tr>
          <td>
            {% if row.parcel_count >= 2 %}
            <a href="/owner/{{ row.cluster_id }}/">{{ row.primary_name | e }}</a>
            {% else %}
            {{ row.primary_name | e }}
            {% endif %}
          </td>
          <td class="num">{{ row.parcel_count }}</td>
          <td class="flags-cell">
            {% if row.is_corporate %}<span class="badge-corporate">CORPORATE</span>{% endif %}
            {% if row.is_institutional %}<span class="badge-institutional">INSTITUTIONAL</span>{% endif %}
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

def slugify(name):
    """'Old Fourth Ward' → 'old-fourth-ward', 'NPU A' → 'npu-a'"""
    s = re.sub(r'[^a-z0-9]+', '-', name.lower())
    return s.strip('-')

def fmt_acres(val):
    if val is None:
        return "—"
    return f"{float(val):,.1f}"

def fmt_int(val):
    if val is None:
        return 0
    return int(val)

def render_leaderboard(rows):
    env = _make_env()
    tmpl = env.from_string(LEADERBOARD_TMPL)
    return tmpl.render(
        page_title="Top Landlords",
        meta_description="The top corporate and institutional property owners in Atlanta, ranked by parcel count across Fulton and DeKalb counties.",
        rows=rows,
        total=len(rows),
    )

def render_owner(cluster_id, stats, parcels, county_breakdown, sos_data, neighborhoods,
                 linkable_agent_ids=frozenset(), cluster_related=None,
                 entity_sos_ids=None, officers=None):
    names = stats["owner_names"] or []
    primary_name = names[0] if names else f"Cluster {cluster_id}"

    # alt_names as [{name, sos_business_id}] — SOS IDs from entity_sos_ids lookup
    sos_id_map = {item["name"]: item["sos_business_id"]
                  for item in (entity_sos_ids or [])}
    alt_names = [
        {"name": n, "sos_business_id": sos_id_map.get(n)}
        for n in sorted(names[1:])
    ] if len(names) > 1 else []

    # Owner addresses — cap at 20, skip empty
    raw_addrs = stats.get("owner_addresses") or []
    owner_addresses = [a for a in raw_addrs if a and a.strip()][:20]

    # County breakdown
    county_fulton = county_breakdown.get("fulton", 0)
    county_dekalb = county_breakdown.get("dekalb", 0)

    # SOS data
    sos_rows = sos_data.get("rows", [])
    sos_statuses = sos_data.get("statuses", [])
    sos_states = sos_data.get("states", [])
    sos_business_types = sos_data.get("business_types", [])
    sos_agents = sos_data.get("agents", [])          # cap handled in fetch
    principal_offices = sos_data.get("principal_offices", [])

    # Related owners
    related_owners = (cluster_related or {}).get(cluster_id, [])

    # Officers (capped in fetch function)
    officers_list = officers or []

    # Parcel table cap
    parcel_count_raw = fmt_int(stats["parcel_count"])
    parcel_table_capped = len(parcels) > 200
    parcels_display = parcels[:200]

    env = _make_env()
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
        principal_offices=principal_offices,
        linkable_agent_ids=linkable_agent_ids,
        related_owners=related_owners,
        neighborhoods=neighborhoods,
        officers=officers_list,
        parcels=parcels_display,
        parcel_table_capped=parcel_table_capped,
    )

# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def ensure_materialized_views(conn):
    """Check if required materialized views exist; if not, run the creation script."""
    required_views = ["mv_leaderboard", "mv_cluster_stats"]
    missing = []
    with conn.cursor() as cur:
        cur.execute("SELECT matviewname FROM pg_matviews")
        existing = [r[0] for r in cur.fetchall()]
        for view in required_views:
            if view not in existing:
                missing.append(view)

    if missing:
        print(f"Materialized views missing ({', '.join(missing)}). Recreating all...")
        sql_path = Path(__file__).parent / "sql" / "04_create_materialized_views.sql"
        if not sql_path.exists():
            print(f"Error: SQL script not found at {sql_path}")
            sys.exit(1)

        with open(sql_path, "r") as f:
            sql = f.read()

        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("Materialized views recreated.")

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
    """Returns {cluster_id: {rows, statuses, states, business_types, agents, principal_offices}}"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT cluster_id,
                   sos_status, sos_foreign_state, sos_business_type,
                   sos_registered_agent, sos_registered_agent_address,
                   sos_registered_agent_id,
                   sos_principal_city, sos_principal_state,
                   COUNT(*) AS entity_count
            FROM owner_entities
            WHERE cluster_id = ANY(%s) AND sos_status IS NOT NULL
            GROUP BY cluster_id, sos_status, sos_foreign_state, sos_business_type,
                     sos_registered_agent, sos_registered_agent_address,
                     sos_registered_agent_id,
                     sos_principal_city, sos_principal_state
            ORDER BY cluster_id, entity_count DESC
        """, (cluster_ids,))
        rows_by_cluster = defaultdict(list)
        for row in cur.fetchall():
            rows_by_cluster[row["cluster_id"]].append(dict(row))

    result = {}
    for cid, rows in rows_by_cluster.items():
        seen_statuses = {}
        seen_states = set()
        seen_types = set()
        seen_agents = {}  # (name_lower, addr_lower) -> {name, address, ra_id}
        seen_principal_offices = {}  # (city_lower, state_lower) -> {display, out_of_state}

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
            ra_id = r["sos_registered_agent_id"]
            if ra_name and ra_name.upper() not in ("NONE", ""):
                key = (ra_name.lower(), ra_addr.lower())
                if key not in seen_agents:
                    seen_agents[key] = {"name": ra_name, "address": ra_addr, "ra_id": ra_id}

            pcity = (r["sos_principal_city"] or "").strip()
            pstate = (r["sos_principal_state"] or "").strip()
            if pcity or pstate:
                po_key = (pcity.lower(), pstate.lower())
                if po_key not in seen_principal_offices:
                    parts = [x for x in [pcity, pstate] if x]
                    display = ", ".join(parts)
                    out_of_state = bool(pstate and pstate not in ("Georgia", "GA"))
                    seen_principal_offices[po_key] = {
                        "city": pcity, "state": pstate,
                        "display": display,
                        "out_of_state": out_of_state,
                    }

        statuses = [s for s, _ in sorted(seen_statuses.items(), key=lambda x: -x[1])]
        agents = list(seen_agents.values())[:20]
        # Sort principal offices: out-of-state first, then alphabetical
        principal_offices = sorted(
            seen_principal_offices.values(),
            key=lambda po: (not po["out_of_state"], po["display"])
        )

        result[cid] = {
            "rows": rows,
            "statuses": statuses,
            "states": sorted(seen_states),
            "business_types": sorted(seen_types),
            "agents": agents,
            "principal_offices": principal_offices,
        }
    return result


def fetch_entity_sos_ids_batch(conn, cluster_ids):
    """Returns {cluster_id: [{name, sos_business_id}]} for entities with SOS matches.
    Used to link owner names directly to their GA SOS filings."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cluster_id, owner_name_norm, sos_business_id
            FROM owner_entities
            WHERE cluster_id = ANY(%s) AND sos_business_id IS NOT NULL
            ORDER BY cluster_id, owner_name_norm
        """, (cluster_ids,))
        by_cluster = defaultdict(list)
        seen_ids = defaultdict(set)
        for row in cur.fetchall():
            cid, name, bid = row
            if bid not in seen_ids[cid]:
                by_cluster[cid].append({"name": name, "sos_business_id": bid})
                seen_ids[cid].add(bid)
    return dict(by_cluster)


def fetch_officers_batch(conn, cluster_ids):
    """Returns {cluster_id: [{role, name, city, state}]}, deduped, capped at 20."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT
                oe.cluster_id,
                o.description AS role,
                o.first_name,
                o.middle_name,
                o.last_name,
                o.company_name,
                o.city,
                o.state
            FROM owner_entities oe
            JOIN sos.officers o ON o.control_number = oe.sos_control_number
            WHERE oe.cluster_id = ANY(%s)
              AND (o.first_name IS NOT NULL OR o.company_name IS NOT NULL)
            ORDER BY oe.cluster_id, o.description, o.last_name, o.first_name
        """, (cluster_ids,))
        by_cluster = defaultdict(list)
        seen = defaultdict(set)
        for row in cur.fetchall():
            cid, role, first, middle, last, company, city, state = row
            # Build display name
            if company and company.strip():
                name = company.strip()
            else:
                parts = [x.strip() for x in [first, middle, last] if x and x.strip()]
                name = " ".join(parts)
            if not name:
                continue
            key = (role, name.lower())
            if key not in seen[cid]:
                seen[cid].add(key)
                by_cluster[cid].append({
                    "role": (role or "").strip(),
                    "name": name,
                    "city": (city or "").strip(),
                    "state": (state or "").strip(),
                })
        # Cap at 20 per cluster
        return {cid: entries[:20] for cid, entries in by_cluster.items()}


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
            by_cluster[cid].append({"name": nbhd, "name_enc": quote_plus(nbhd), "count": int(count)})

    return dict(by_cluster)

def fetch_linkable_agent_ids(conn):
    """Returns {ra_id: {name, cluster_count}} for individual (non-commercial) RAs in ≥2 clusters."""
    blocklist_clauses = " AND ".join(
        f"oe.sos_registered_agent NOT ILIKE %s" for _ in COMMERCIAL_RA_PATTERNS
    )
    sql = f"""
        SELECT oe.sos_registered_agent_id AS ra_id,
               MAX(oe.sos_registered_agent) AS ra_name,
               COUNT(DISTINCT oe.cluster_id) AS cluster_count
        FROM owner_entities oe
        WHERE oe.sos_registered_agent IS NOT NULL
          AND oe.sos_registered_agent != ''
          AND oe.sos_registered_agent != 'NONE'
          AND {blocklist_clauses}
        GROUP BY oe.sos_registered_agent_id
        HAVING COUNT(DISTINCT oe.cluster_id) >= 2
    """
    with conn.cursor() as cur:
        cur.execute(sql, COMMERCIAL_RA_PATTERNS)
        result = {}
        for row in cur.fetchall():
            ra_id, ra_name, cluster_count = row
            if not is_commercial_ra(ra_name):
                result[ra_id] = {"name": ra_name, "cluster_count": int(cluster_count)}
        return result


def fetch_agent_clusters(conn, ra_ids):
    """Returns {ra_id: [{cluster_id, primary_name, parcel_count, is_corporate, is_institutional}, ...]}."""
    if not ra_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT oe.sos_registered_agent_id AS ra_id,
                   oc.cluster_id, oc.owner_names[1] AS primary_name, oc.parcel_count,
                   (mc.corporate_parcel_count > 0) AS is_corporate,
                   (mc.institutional_parcel_count > 0) AS is_institutional
            FROM owner_entities oe
            JOIN ownership_clusters oc ON oc.cluster_id = oe.cluster_id
            JOIN mv_cluster_stats mc ON mc.cluster_id = oe.cluster_id
            WHERE oe.sos_registered_agent_id = ANY(%s)
            GROUP BY oe.sos_registered_agent_id, oc.cluster_id, oc.owner_names[1], oc.parcel_count,
                     mc.corporate_parcel_count, mc.institutional_parcel_count
            ORDER BY oe.sos_registered_agent_id, oc.parcel_count DESC
        """, (list(ra_ids),))
        result = defaultdict(list)
        for row in cur.fetchall():
            ra_id, cluster_id, primary_name, parcel_count, is_corp, is_inst = row
            result[ra_id].append({
                "cluster_id": cluster_id,
                "primary_name": primary_name or f"Cluster {cluster_id}",
                "parcel_count": int(parcel_count),
                "is_corporate": bool(is_corp),
                "is_institutional": bool(is_inst),
            })
        return result


def fetch_address_linkage(conn):
    """Returns {addr: [{cluster_id, primary_name, parcel_count, is_corporate, is_institutional}, ...]}
    for addresses shared by 2–10 clusters (real street addresses — must start with a digit)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT addr, oc.cluster_id, oc.owner_names[1] AS primary_name, oc.parcel_count,
                   (mc.corporate_parcel_count > 0) AS is_corporate,
                   (mc.institutional_parcel_count > 0) AS is_institutional
            FROM ownership_clusters oc
            JOIN mv_cluster_stats mc ON mc.cluster_id = oc.cluster_id,
            unnest(oc.owner_addresses) AS addr
            WHERE oc.owner_addresses IS NOT NULL
              AND addr ~ '^[0-9]'
              AND addr IN (
                SELECT addr
                FROM ownership_clusters, unnest(owner_addresses) AS addr
                WHERE owner_addresses IS NOT NULL AND addr ~ '^[0-9]'
                GROUP BY addr
                HAVING COUNT(DISTINCT cluster_id) BETWEEN 2 AND 10
              )
            ORDER BY addr, oc.parcel_count DESC
        """)
        result = defaultdict(list)
        for row in cur.fetchall():
            addr, cluster_id, primary_name, parcel_count, is_corp, is_inst = row
            result[addr].append({
                "cluster_id": cluster_id,
                "primary_name": primary_name or f"Cluster {cluster_id}",
                "parcel_count": int(parcel_count),
                "is_corporate": bool(is_corp),
                "is_institutional": bool(is_inst),
            })
        return result


def fetch_geo_data(conn, col_name):
    """Fetch (area, cluster_id, primary_name, flags, local_parcel_count) for one
    Atlanta geographic field ('city_neighborhood', 'city_council', 'city_npu').
    Returns {area: [rows sorted by local_parcel_count desc]}.
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            WITH area_map AS (
                SELECT parcelid, {col_name} AS area FROM fulton_parcels WHERE {col_name} IS NOT NULL
                UNION ALL
                SELECT parcelid, {col_name} FROM dekalb_parcels WHERE {col_name} IS NOT NULL
            )
            SELECT am.area,
                   oe.cluster_id,
                   mc.owner_names[1] AS primary_name,
                   mc.owner_names[2:4] AS alt_names_arr,
                   mc.corporate_parcel_count > 0 AS is_corporate,
                   mc.institutional_parcel_count > 0 AS is_institutional,
                   mc.primary_foreign_state,
                   mc.parcel_count AS total_parcel_count,
                   COUNT(*) AS local_parcel_count
            FROM owner_entities oe
            CROSS JOIN LATERAL unnest(oe.parcel_ids) AS u(pid)
            JOIN area_map am ON am.parcelid = u.pid
            JOIN mv_cluster_stats mc ON mc.cluster_id = oe.cluster_id
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
            ORDER BY area, local_parcel_count DESC
        """)
        by_area = defaultdict(list)
        for row in cur.fetchall():
            area, cid, name, alt_arr, is_corp, is_inst, fstate, total_count, local_count = row
            alts = [n for n in (alt_arr or []) if n]
            by_area[area].append({
                "cluster_id": cid,
                "primary_name": name or f"Cluster {cid}",
                "alt_names": ", ".join(alts) if alts else "",
                "is_corporate": bool(is_corp),
                "is_institutional": bool(is_inst),
                "foreign_state": fstate,
                "total_parcel_count": int(total_count),
                "local_parcel_count": int(local_count),
                "connection_count": 0,  # filled in below if cluster_connection_count passed
            })
        return dict(by_area)


def fetch_county_geo_data(conn):
    """Top owners by parcel count per county. Returns {county: [rows]}."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT oe.county,
                   oe.cluster_id,
                   mc.owner_names[1] AS primary_name,
                   mc.owner_names[2:4] AS alt_names_arr,
                   mc.corporate_parcel_count > 0 AS is_corporate,
                   mc.institutional_parcel_count > 0 AS is_institutional,
                   mc.primary_foreign_state,
                   mc.parcel_count AS total_parcel_count,
                   SUM(oe.count) AS local_parcel_count
            FROM owner_entities oe
            JOIN mv_cluster_stats mc ON mc.cluster_id = oe.cluster_id
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
            ORDER BY county, local_parcel_count DESC
        """)
        by_county = defaultdict(list)
        for row in cur.fetchall():
            county, cid, name, alt_arr, is_corp, is_inst, fstate, total_count, local_count = row
            alts = [n for n in (alt_arr or []) if n]
            by_county[county].append({
                "cluster_id": cid,
                "primary_name": name or f"Cluster {cid}",
                "alt_names": ", ".join(alts) if alts else "",
                "is_corporate": bool(is_corp),
                "is_institutional": bool(is_inst),
                "foreign_state": fstate,
                "total_parcel_count": int(total_count),
                "local_parcel_count": int(local_count),
                "connection_count": 0,
            })
        return dict(by_county)


def build_cluster_related(linkable_agents, agent_clusters, address_groups=None):
    """Compute related-cluster lists from RA co-membership and shared mailing addresses.

    Returns:
        cluster_related: {cluster_id: [{cluster_id, primary_name, parcel_count, via_items}, ...]}
            sorted by parcel_count desc, capped at 15.
            via_items: [{text, url}] — each connection reason with optional internal link.
        cluster_connection_count: {cluster_id: N} (pre-cap total, for leaderboard badge)
    """
    # staging[cid][ocid] = {primary_name, parcel_count, via_reasons: {text: url_or_None}}
    staging = defaultdict(dict)

    def _link(cid, other_c, reason_text, reason_url=None):
        ocid = other_c["cluster_id"]
        if ocid not in staging[cid]:
            staging[cid][ocid] = {
                "cluster_id": ocid,
                "primary_name": other_c["primary_name"],
                "parcel_count": other_c["parcel_count"],
                "via_reasons": {},
            }
        # Keep first URL seen for a given reason text
        if reason_text not in staging[cid][ocid]["via_reasons"]:
            staging[cid][ocid]["via_reasons"][reason_text] = reason_url

    for ra_id, clusters in agent_clusters.items():
        agent_name = linkable_agents[ra_id]["name"]
        agent_url = f"/agent/{ra_id}/"
        reason = f"Shared RA: {agent_name}"
        for this_c in clusters:
            for other_c in clusters:
                if this_c["cluster_id"] != other_c["cluster_id"]:
                    _link(this_c["cluster_id"], other_c, reason, agent_url)

    for addr, clusters in (address_groups or {}).items():
        addr_slug = slugify(addr)
        addr_url = f"/l/addresses/{addr_slug}/"
        reason = f"Shared address: {addr}"
        for this_c in clusters:
            for other_c in clusters:
                if this_c["cluster_id"] != other_c["cluster_id"]:
                    _link(this_c["cluster_id"], other_c, reason, addr_url)

    cluster_related = {}
    cluster_connection_count = {}
    for cid, others in staging.items():
        rows = []
        for ocid, info in others.items():
            via_items = [
                {"text": text, "url": url}
                for text, url in sorted(info["via_reasons"].items())
            ]
            rows.append({
                "cluster_id": ocid,
                "primary_name": info["primary_name"],
                "parcel_count": info["parcel_count"],
                "via_items": via_items,
            })
        rows.sort(key=lambda r: -r["parcel_count"])
        cluster_connection_count[cid] = len(rows)
        cluster_related[cid] = rows[:15]

    return cluster_related, cluster_connection_count


def write_if_changed(path, content):
    """Write content to path only if it differs from current content."""
    path = Path(path)
    if path.exists():
        try:
            if path.read_text() == content:
                return False
        except Exception:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_leaderboard(conn, output_dir, cluster_connection_count=None):
    print("Building leaderboard...", end=" ", flush=True)
    rows_raw = fetch_leaderboard(conn)
    counts = cluster_connection_count or {}

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
            "connection_count": counts.get(r["cluster_id"], 0),
        })

    html = render_leaderboard(rows)
    for dest in [output_dir / "l" / "index.html",
                 output_dir / "leaderboard" / "index.html"]:
        write_if_changed(dest, html)
    print(f"done ({len(rows)} rows)")

def build_agent_pages(linkable_agents, agent_clusters, output_dir):
    """Generate /agent/{ra_id}/index.html for each linkable registered agent,
    plus /agents/index.html listing all of them."""
    env = _make_env()
    tmpl = env.from_string(AGENT_TMPL)
    index_tmpl = env.from_string(AGENTS_INDEX_TMPL)
    written = 0

    index_rows = []
    for ra_id, info in linkable_agents.items():
        clusters = agent_clusters.get(ra_id, [])
        total_parcels = sum(c["parcel_count"] for c in clusters)
        html = tmpl.render(
            page_title=f"{info['name']} — Registered Agent",
            meta_description=f"{info['name']} is a registered agent for {info['cluster_count']} owner clusters in Atlanta.",
            agent_name=info["name"],
            cluster_count=len(clusters),
            total_parcels=total_parcels,
            clusters=clusters,
        )
        out_path = output_dir / "agent" / str(ra_id) / "index.html"
        write_if_changed(out_path, html)
        written += 1
        index_rows.append({
            "ra_id": ra_id,
            "name": info["name"],
            "cluster_count": info["cluster_count"],
            "total_parcels": total_parcels,
        })

    index_rows.sort(key=lambda r: (-r["cluster_count"], r["name"]))
    index_html = index_tmpl.render(
        page_title="Registered Agents",
        meta_description="Individual registered agents appearing across multiple owner clusters in Atlanta.",
        rows=index_rows,
        total=len(index_rows),
    )
    for dest in [output_dir / "l" / "agents" / "index.html",
                 output_dir / "agents" / "index.html"]:
        write_if_changed(dest, index_html)

    return written


def build_address_pages(address_groups, output_dir):
    """Generate /l/addresses/{slug}/index.html for each shared mailing address,
    plus /l/addresses/index.html listing all of them."""
    env = _make_env()
    tmpl = env.from_string(ADDRESS_TMPL)
    index_tmpl = env.from_string(ADDRESS_INDEX_TMPL)
    written = 0
    index_rows = []

    for addr, clusters in address_groups.items():
        slug = slugify(addr)
        total_parcels = sum(c["parcel_count"] for c in clusters)
        html = tmpl.render(
            page_title=f"{addr} — Shared Address",
            meta_description=f"{len(clusters)} owner clusters share the mailing address {addr}.",
            address=addr,
            cluster_count=len(clusters),
            total_parcels=total_parcels,
            clusters=clusters,
        )
        out_path = output_dir / "l" / "addresses" / slug / "index.html"
        write_if_changed(out_path, html)
        written += 1
        index_rows.append({
            "address": addr,
            "slug": slug,
            "cluster_count": len(clusters),
            "total_parcels": total_parcels,
        })

    index_rows.sort(key=lambda r: (-r["cluster_count"], r["address"]))
    index_html = index_tmpl.render(
        page_title="Shared Mailing Addresses",
        meta_description="Street addresses shared by multiple distinct property owner clusters in Atlanta.",
        rows=index_rows,
        total=len(index_rows),
    )
    for dest in [output_dir / "l" / "addresses" / "index.html",
                 output_dir / "addresses" / "index.html"]:
        write_if_changed(dest, index_html)

    print(f"  address pages: {written} pages + index")
    return written


def _build_geo_section(env, area_rows, output_dir, url_base, geo_type_label, area_label,
                       index_title, index_lead, area_display_fn=None, geo_key=None,
                       cluster_connection_count=None):
    """Build individual area pages + index page for one geo dimension.
    area_display_fn: optional callable(raw_area) -> display string (e.g. 'District 5')
    Returns number of area pages written.
    """
    geo_tmpl = env.from_string(GEO_LEADERBOARD_TMPL)
    idx_tmpl = env.from_string(GEO_INDEX_TMPL)
    index_url = f"/{url_base}/"
    counts = cluster_connection_count or {}
    written = 0
    index_rows = []

    for area, rows in area_rows.items():
        slug = slugify(str(area))
        display = area_display_fn(area) if area_display_fn else str(area)
        area_total = sum(r["local_parcel_count"] for r in rows)
        area_raw_enc = quote_plus(str(area)) if geo_key else ""
        area_map_url = f"/?geo={geo_key}&area={area_raw_enc}" if geo_key else ""

        # Filter out single-parcel owners (homeowners, not portfolios)
        filtered = [r for r in rows if r["total_parcel_count"] > 1]
        top100 = filtered[:100]
        # Inject connection counts
        for r in top100:
            r["connection_count"] = counts.get(r["cluster_id"], 0)

        html = geo_tmpl.render(
            page_title=f"{display} — Top Property Owners",
            meta_description=f"Top property owners in {display}, ranked by local parcel count.",
            area_name=display,
            geo_type_label=geo_type_label,
            index_url=index_url,
            index_label=index_title,
            geo_key=geo_key,
            area_raw_enc=area_raw_enc,
            area_map_url=area_map_url,
            rows=top100,
            total=len(top100),
            area_total_parcels=area_total,
        )
        out_path = output_dir / slug / "index.html"
        write_if_changed(out_path, html)
        written += 1
        index_rows.append({
            "area": display,
            "url": f"/{url_base}/{slug}/",
            "total_parcels": area_total,
            "top_owner": filtered[0]["primary_name"] if filtered else "",
            "map_url": area_map_url,
        })

    index_rows.sort(key=lambda r: r["area"])
    index_html = idx_tmpl.render(
        page_title=index_title,
        meta_description=index_lead,
        index_title=index_title,
        index_lead=index_lead,
        area_label=area_label,
        rows=index_rows,
        total=len(index_rows),
    )
    idx_path = output_dir / "index.html"
    write_if_changed(idx_path, index_html)

    return written


def build_geo_leaderboard_pages(conn, output_dir, cluster_connection_count=None):
    """Generate all geo leaderboard pages under /l/."""
    env = _make_env()
    base = output_dir / "l"

    print("Building geo leaderboards...")

    # Atlanta neighborhoods
    print("  neighborhood...", end=" ", flush=True)
    nbhd_data = fetch_geo_data(conn, "city_neighborhood")
    n = _build_geo_section(
        env, nbhd_data, base / "atlanta" / "neighborhood",
        url_base="l/atlanta/neighborhood",
        geo_type_label="Atlanta neighborhood",
        area_label="Neighborhood",
        index_title="Atlanta Neighborhoods",
        index_lead="Top property owners by Atlanta neighborhood.",
        geo_key="neighborhood",
        cluster_connection_count=cluster_connection_count,
    )
    print(f"{n} pages")

    # Atlanta council districts
    print("  council...", end=" ", flush=True)
    council_data = fetch_geo_data(conn, "city_council")
    n = _build_geo_section(
        env, council_data, base / "atlanta" / "council",
        url_base="l/atlanta/council",
        geo_type_label="Atlanta council district",
        area_label="District",
        index_title="Atlanta City Council Districts",
        index_lead="Top property owners by Atlanta City Council district.",
        area_display_fn=lambda v: f"District {v}",
        geo_key="council",
        cluster_connection_count=cluster_connection_count,
    )
    print(f"{n} pages")

    # Atlanta NPUs
    print("  npu...", end=" ", flush=True)
    npu_data = fetch_geo_data(conn, "city_npu")
    n = _build_geo_section(
        env, npu_data, base / "atlanta" / "npu",
        url_base="l/atlanta/npu",
        geo_type_label="Atlanta NPU",
        area_label="NPU",
        index_title="Atlanta NPUs",
        index_lead="Top property owners by Atlanta Neighborhood Planning Unit (NPU).",
        area_display_fn=lambda v: f"NPU {v}",
        geo_key="npu",
        cluster_connection_count=cluster_connection_count,
    )
    print(f"{n} pages")

    # County leaderboards
    print("  county...", end=" ", flush=True)
    county_data = fetch_county_geo_data(conn)
    env2 = _make_env()
    geo_tmpl = env2.from_string(GEO_LEADERBOARD_TMPL)
    counts = cluster_connection_count or {}
    for county, rows in county_data.items():
        county_total = sum(r["local_parcel_count"] for r in rows)
        filtered = [r for r in rows if r["total_parcel_count"] > 1]
        top500 = filtered[:500]
        for r in top500:
            r["connection_count"] = counts.get(r["cluster_id"], 0)
        html = geo_tmpl.render(
            page_title=f"{county.title()} County — Top Property Owners",
            meta_description=f"Top property owners in {county.title()} County by parcel count.",
            area_name=f"{county.title()} County",
            geo_type_label="county",
            index_url="/l/",
            index_label="Leaderboards",
            area_map_url="",
            rows=top500,
            total=len(top500),
            area_total_parcels=county_total,
        )
        out_path = base / county / "index.html"
        write_if_changed(out_path, html)
    print(f"{len(county_data)} pages")


def worker(args):
    """Worker function run in a subprocess. Processes a slice of cluster_ids."""
    cluster_ids, output_dir, db_url, worker_id, linkable_agent_ids, cluster_related = args
    output_dir = Path(output_dir)
    written = 0

    conn = psycopg2.connect(db_url)
    try:
        for i in range(0, len(cluster_ids), BATCH_SIZE):
            batch = cluster_ids[i:i + BATCH_SIZE]
            stats_map      = fetch_cluster_stats_batch(conn, batch)
            parcels_map    = fetch_parcels_batch(conn, batch)
            county_map     = fetch_county_breakdown_batch(conn, batch)
            sos_map        = fetch_sos_details_batch(conn, batch)
            nbhd_map       = fetch_neighborhood_concentration_batch(conn, batch)
            sos_ids_map    = fetch_entity_sos_ids_batch(conn, batch)
            officers_map   = fetch_officers_batch(conn, batch)

            for cid in batch:
                stats = stats_map.get(cid)
                if not stats:
                    continue
                parcels        = parcels_map.get(cid, [])
                county_breakdown = county_map.get(cid, {})
                sos_data       = sos_map.get(cid, {})
                neighborhoods  = nbhd_map.get(cid, [])
                entity_sos_ids = sos_ids_map.get(cid, [])
                officers       = officers_map.get(cid, [])
                html = render_owner(
                    cid, stats, parcels, county_breakdown, sos_data, neighborhoods,
                    linkable_agent_ids, cluster_related,
                    entity_sos_ids=entity_sos_ids,
                    officers=officers,
                )
                out_path = output_dir / "owner" / str(cid) / "index.html"
                write_if_changed(out_path, html)
                written += 1
    finally:
        conn.close()

    return written


def build_owner_pages(conn, output_dir, min_parcels, num_workers, cluster_ids_override=None, linkable_agent_ids=frozenset(), cluster_related=None):
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
    work_args = [(chunk, str(output_dir), DB_URL, i, linkable_agent_ids, cluster_related or {}) for i, chunk in enumerate(chunks)]

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
        ensure_materialized_views(conn)

        # Fetch linkable agent data once — used by both agent pages and owner pages
        print("Fetching linkable registered agents...", end=" ", flush=True)
        linkable_agents = fetch_linkable_agent_ids(conn)
        linkable_agent_ids = frozenset(linkable_agents.keys())
        print(f"{len(linkable_agents)} individual RAs across ≥2 clusters")

        agent_clusters = fetch_agent_clusters(conn, linkable_agent_ids)

        print("Fetching shared mailing address groups...", end=" ", flush=True)
        address_groups = fetch_address_linkage(conn)
        print(f"{len(address_groups)} addresses shared by 2–10 clusters")

        cluster_related, cluster_connection_count = build_cluster_related(
            linkable_agents, agent_clusters, address_groups)

        if not args.owner_only:
            build_leaderboard(conn, output_dir, cluster_connection_count)
            build_geo_leaderboard_pages(conn, output_dir,
                                        cluster_connection_count=cluster_connection_count)
            # Agent pages are fast; always build unless owner-only
            print("Building agent pages...", end=" ", flush=True)
            n_agents = build_agent_pages(linkable_agents, agent_clusters, output_dir)
            print(f"done ({n_agents} pages)")
            # Shared address pages
            print("Building shared address pages...", end=" ", flush=True)
            build_address_pages(address_groups, output_dir)

        if not args.leaderboard_only:
            build_owner_pages(conn, output_dir, args.min_parcels, args.workers,
                              cluster_ids_override=cluster_ids_override,
                              linkable_agent_ids=linkable_agent_ids,
                              cluster_related=cluster_related)
    finally:
        conn.close()

    print("All done.")

if __name__ == "__main__":
    main()
