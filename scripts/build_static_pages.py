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
from collections import defaultdict, Counter
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
HOME_TYPES_ORDER = ["Single-Family", "Multi-Family / Other", "Multi-Family / Condo", "Other"]

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
    env = Environment(loader=BaseLoader(), autoescape=True)
    env.filters['urlencode'] = lambda s: quote_plus(str(s)) if s else ''
    env.filters['format_int'] = lambda v: f"{int(v):,}" if v is not None else "0"
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
      <a href="/numbers/">By The Numbers</a>
      <a href="/about/">About</a>
      <a href="/methodology/">Methodology</a>
      <a href="/faq/">FAQ</a>
    </nav>
    <div class="last-updated">
      Last updated: {{ last_updated_str }}
    </div>
  </footer>
</body>
</html>
"""

NUMBERS_TMPL = """\
<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>By the Numbers — Who Owns Atlanta?</title>
  <meta name="description" content="How corporate, institutional, and individual property ownership in Atlanta maps onto neighborhood income, race, and housing conditions.">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.classless.min.css">
  <link rel="stylesheet" href="/css/style.css">
  <link rel="stylesheet" href="/css/content.css">
  <style>
    .leaderboard-table td.cell-high { background-color: rgba(99, 102, 241, 0.2); }
    .leaderboard-table td.cell-med  { background-color: rgba(99, 102, 241, 0.1); }
    .leaderboard-table td.cell-low  { background-color: rgba(99, 102, 241, 0.03); }
    /* Ensure number alignment and padding stay consistent */
    .leaderboard-table td.num { text-align: right; padding-right: 1.5rem; }
    .bucket-label { text-align: left; white-space: nowrap; }
    .bucket-row { grid-template-columns: 5.5rem 1fr 2.5rem; }
  </style>
</head>
<body class="content-page">
  <header>
    <a href="/" class="site-name">Who Owns Atlanta?</a>
    <nav class="header-nav">
      <a href="/l/">Leaderboards</a>
    </nav>
  </header>

  <main class="content-main content-prose">
    <h1>By the Numbers</h1>

    <p class="lead">
      Corporate-owned parcels in Atlanta are concentrated in lower-income, majority-Black neighborhoods
      at significantly higher rates than individually-owned parcels.
      Across {{ totals.total_parcels | int | format_int }} city parcels with neighborhood data,
      the average corporate-owned parcel sits in a neighborhood with a median household income of
      <strong>${{ totals.corp_income | int | format_int }}</strong> — versus
      <strong>${{ totals.indiv_income | int | format_int }}</strong> for individually-owned parcels.
      Corporate portfolios average <strong>{{ totals.corp_black | round(1) }}%</strong> Black neighborhoods
      compared to <strong>{{ totals.indiv_black | round(1) }}%</strong> for individual owners.
      By income quartile, <strong>{{ totals.corp_q12_pct }}%</strong> of corporate parcels fall in the
      bottom half of Atlanta neighborhoods — versus <strong>{{ totals.indiv_q12_pct }}%</strong> for
      individually-owned parcels.
    </p>

    <h2>Ownership Type Summary</h2>
    <div class="table-scroll">
    <table class="leaderboard-table">
      <thead>
        <tr>
          <th>Metric</th>
          <th class="num">Corporate</th>
          <th class="num">Institutional</th>
          <th class="num">Individual</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>Parcel count</td>
          {% for r in by_type %}
          <td class="num {{ r.parcel_count_class }}">{{ r.parcel_count | format_int }}
            <span style="opacity:0.55;font-size:0.8em">({{ (r.parcel_count / totals.total_parcels * 100) | round(1) }}%)</span>
          </td>
          {% endfor %}
        </tr>
        <tr>
          <td>Avg portfolio size</td>
          {% for r in by_type %}
          <td class="num {{ r.avg_portfolio_size_class }}">{{ r.avg_portfolio_size | round(1) }} <span style="opacity:0.55;font-size:0.8em">parcels</span></td>
          {% endfor %}
        </tr>
        <tr>
          <td>% Out-of-State (Matched)</td>
          {% for r in by_type %}
          <td class="num {{ r.pct_out_of_state_class }}">{{ r.pct_out_of_state | round(1) }}%</td>
          {% endfor %}
        </tr>
        <tr>
          <td>Median neighborhood income</td>
          {% for r in by_type %}
          <td class="num {{ r.median_neighborhood_income_class }}">${{ r.median_neighborhood_income | int | format_int }}</td>
          {% endfor %}
        </tr>
        <tr>
          <td>Avg neighborhood median income</td>
          {% for r in by_type %}
          <td class="num {{ r.avg_neighborhood_income_class }}">${{ r.avg_neighborhood_income | int | format_int }}</td>
          {% endfor %}
        </tr>
        <tr>
          <td>Avg neighborhood median home value</td>
          {% for r in by_type %}
          <td class="num {{ r.avg_neighborhood_home_value_class }}">${{ r.avg_neighborhood_home_value | int | format_int }}</td>
          {% endfor %}
        </tr>
        <tr>
          <td>Avg % Bachelor's Degree</td>
          {% for r in by_type %}
          <td class="num {{ r.avg_bachelors_pct_class }}">{{ r.avg_bachelors_pct | round(1) }}%</td>
          {% endfor %}
        </tr>
        <tr>
          <td>Avg renter %</td>
          {% for r in by_type %}
          <td class="num {{ r.avg_renter_pct_class }}">{{ r.avg_renter_pct | round(1) }}%</td>
          {% endfor %}
        </tr>
        <tr>
          <td>Avg poverty rate</td>
          {% for r in by_type %}
          <td class="num {{ r.avg_poverty_pct_class }}">{{ r.avg_poverty_pct | round(1) }}%</td>
          {% endfor %}
        </tr>
        <tr>
          <td>Avg vacancy rate</td>
          {% for r in by_type %}
          <td class="num {{ r.avg_vacant_pct_class }}">{{ r.avg_vacant_pct | round(1) }}%</td>
          {% endfor %}
        </tr>
      </tbody>
    </table>
    </div>

    <h2>Racial Composition of Neighborhoods</h2>
    <p>Average racial composition of neighborhoods where each ownership type holds property.</p>

    <div class="demographics-grid" style="grid-template-columns: 1fr 1fr 1fr;">
      {% for r in by_type %}
      {% set other_pct = [0, 100 - r.avg_black_pct - r.avg_white_pct - r.avg_hispanic_pct - r.avg_asian_pct] | max %}
      <div class="demo-card">
        <h3>{{ r.owner_type | capitalize }}</h3>
        <div class="race-bar">
          {% if r.avg_black_pct > 0    %}<div class="race-segment" style="width:{{ r.avg_black_pct | round(1) }}%;    background:#6366f1;" title="Black {{ r.avg_black_pct | round(1) }}%"></div>{% endif %}
          {% if r.avg_white_pct > 0    %}<div class="race-segment" style="width:{{ r.avg_white_pct | round(1) }}%;    background:#94a3b8;" title="White {{ r.avg_white_pct | round(1) }}%"></div>{% endif %}
          {% if r.avg_hispanic_pct > 0 %}<div class="race-segment" style="width:{{ r.avg_hispanic_pct | round(1) }}%; background:#f59e0b;" title="Hispanic {{ r.avg_hispanic_pct | round(1) }}%"></div>{% endif %}
          {% if r.avg_asian_pct > 0    %}<div class="race-segment" style="width:{{ r.avg_asian_pct | round(1) }}%;    background:#10b981;" title="Asian {{ r.avg_asian_pct | round(1) }}%"></div>{% endif %}
          {% if other_pct > 0          %}<div class="race-segment" style="width:{{ other_pct | round(1) }}%;           background:#e2e8f0;" title="Other {{ other_pct | round(1) }}%"></div>{% endif %}
        </div>
        <div class="race-legend">
          {% if r.avg_black_pct > 0    %}<span class="race-legend-item"><span class="race-dot" style="background:#6366f1"></span>{{ r.avg_black_pct | round(1) }}% Black</span>{% endif %}
          {% if r.avg_white_pct > 0    %}<span class="race-legend-item"><span class="race-dot" style="background:#94a3b8"></span>{{ r.avg_white_pct | round(1) }}% White</span>{% endif %}
          {% if r.avg_hispanic_pct > 0 %}<span class="race-legend-item"><span class="race-dot" style="background:#f59e0b"></span>{{ r.avg_hispanic_pct | round(1) }}% Hispanic</span>{% endif %}
          {% if r.avg_asian_pct > 0    %}<span class="race-legend-item"><span class="race-dot" style="background:#10b981"></span>{{ r.avg_asian_pct | round(1) }}% Asian</span>{% endif %}
          {% if other_pct > 0          %}<span class="race-legend-item"><span class="race-dot" style="background:#e2e8f0"></span>{{ other_pct | round(1) }}% Other</span>{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>

    <h2>Where Each Ownership Type Concentrates</h2>
    <p>Share of each ownership type's parcels by neighborhood income quartile (Q1 = lowest income, Q4 = highest).</p>

    <div class="demographics-grid" style="grid-template-columns: 1fr 1fr 1fr;">
      {% for r in by_type %}
      {% set type_total = quartile_data[r.owner_type] | sum(attribute='parcel_count') %}
      <div class="demo-card">
        <h3>{{ r.owner_type | capitalize }}</h3>
        <div class="income-buckets">
          {% for q in quartile_data[r.owner_type] %}
          {% set pct = (q.parcel_count / type_total * 100) | round(1) if type_total > 0 else 0 %}
          <div class="bucket-row">
            <span class="bucket-label">Q{{ q.income_quartile }} <span style="opacity:0.6;font-size:0.7em">{% if q.income_quartile == 4 %}&gt; ${{ (q.income_quartile_min / 1000) | round(0) | int }}k{% else %}&lt; ${{ (q.income_quartile_max / 1000) | round(0) | int }}k{% endif %}</span></span>
            <div class="bucket-bar-bg">
              <div class="bucket-bar" style="width:{{ pct }}%"></div>
            </div>
            <span class="bucket-count">{{ pct }}%</span>
          </div>
          {% endfor %}
        </div>
      </div>
      {% endfor %}
    </div>

    <h2>Highest Corporate Concentration</h2>
    <p>Neighborhoods where corporate entities own the highest percentage of all residential parcels.</p>
    <div class="table-scroll">
    <table class="leaderboard-table">
      <thead>
        <tr>
          <th>Neighborhood</th>
          <th class="num">Total Parcels</th>
          <th class="num">Corporate Parcels</th>
          <th class="num">Corporate %</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for n in top_neighborhoods %}
        <tr>
          <td><a href="/l/atlanta/neighborhood/{{ n.slug }}/">{{ n.name }}</a></td>
          <td class="num">{{ n.total_parcels | format_int }}</td>
          <td class="num">{{ n.corporate_parcels | format_int }}</td>
          <td class="num">{{ n.pct_corporate }}%</td>
          <td class="map-link-cell"><a href="/?geo=neighborhood&area={{ n.name_enc }}" class="map-link-small">map →</a></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <h2>Notes</h2>
    <ul>
      <li><strong>Coverage:</strong> Atlanta city parcels only (those with a <code>city_neighborhood</code> assigned), approximately {{ totals.total_parcels | int | format_int }} of ~616k total county parcels.</li>
      <li><strong>Neighborhood demographics:</strong> 2024 vintage (2020 Census base + ACS estimates), covering 248 neighborhoods.</li>
      <li><strong>"Other" race</strong> is the remainder after Black + White + Hispanic + Asian and may include multiracial and Native American households.</li>
      <li><strong>Q4 corporate poverty anomaly:</strong> The Q4 (highest-income) corporate figure shows an elevated poverty rate (~11%) compared to individual owners (~0.5%). This likely reflects LIHTC (Low Income Housing Tax Credit) and other subsidized apartment portfolios operating in otherwise high-value neighborhoods — properties serving low-income tenants whose poverty rates pull up the corporate average despite the high neighborhood median income.</li>
      <li>See <a href="/methodology/">Methodology</a> for full data provenance and known limitations.</li>
    </ul>
  </main>

  <footer>
    <nav>
      <a href="/">Map</a>
      <a href="/l/">Leaderboards</a>
      <a href="/numbers/">By the Numbers</a>
      <a href="/about/">About</a>
      <a href="/methodology/">Methodology</a>
      <a href="/faq/">FAQ</a>
    </nav>
    <div class="last-updated">
      Last updated: {{ last_updated_str }}
    </div>
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
        <a href="/l/agents/">Agents</a>
        <a href="/l/addresses/">Addresses</a>
      </div>
      <div class="subnav-group">
        <span class="subnav-label">County</span>
        <a href="/l/county/fulton/">Fulton</a>
        <a href="/l/county/dekalb/">DeKalb</a>
      </div>
      <div class="subnav-group">
        <span class="subnav-label">Atlanta</span>
        <a href="/l/atlanta/council/">Council</a>
        <a href="/l/atlanta/npu/">NPU</a>
        <a href="/l/atlanta/neighborhood/">Neighborhood</a>
        <a href="/l/atlanta/zoning/">Zoning</a>
      </div>
    </nav>

    <h2>Global — Top Landlords in Atlanta</h2>
    <p class="lead">Ranked by parcel count across Fulton and DeKalb counties.
      <span class="muted">Top {{ total }} owners shown.</span></p>

    <div class="table-scroll">
    <table class="leaderboard-table">
      <thead>
        <tr>
          <th>#</th>
          <th>Owner</th>
          <th class="num">Parcels <span class="cap-note" style="text-transform:none; font-weight:400; opacity:0.7">(City / Total)</span></th>
          <th class="num">Acres</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr class="owner-main-row">
          <td class="rank">{{ loop.index }}</td>
          <td class="owner-cell">
            <div class="owner-name-row">
              <a href="/owner/{{ r.cluster_id }}/">{{ r.primary_name | e }}</a>
              {% if r.income_spark %}
              <span class="income-spark" title="Atlanta portfolio: income distribution Low→High">{% for seg in r.income_spark %}<span style="width:{{ seg.pct }}%;background:{{ seg.color }}"></span>{% endfor %}</span>
              {% endif %}
            </div>
            {% if r.alt_names %}
            <div class="alt-names">{{ r.alt_names | e }}</div>
            {% endif %}
          </td>
          <td class="num">
            <span class="city-count" style="font-weight:600">{{ r.atlanta_parcel_count }}</span>
            <span class="count-separator" style="opacity:0.4; margin:0 2px">/</span>
            <span class="total-count" style="opacity:0.8">{{ r.parcel_count }}</span>
          </td>
          <td class="num">{{ r.acres }}</td>
        </tr>
        {% if r.is_corporate or r.is_institutional or r.foreign_states %}
        <tr class="owner-badges-row-tr">
          <td></td>
          <td colspan="3">
            <div class="leaderboard-badges-row">
              {% if r.is_corporate %}<span class="badge-corporate">CORPORATE</span>{% endif %}
              {% if r.is_institutional %}<span class="badge-institutional">INSTITUTIONAL</span>{% endif %}
              {% if r.foreign_states %}
                {% for st in r.foreign_states %}
                <span class="badge-state">{{ st | upper | e }}</span>
                {% endfor %}
              {% endif %}
            </div>
          </td>
        </tr>
        {% endif %}
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

    {% if badges %}
    <div class="owner-badges-row">
      {% for b in badges %}<span class="{{ b.class }}">{{ b.label | e }}</span>{% endfor %}
    </div>
    {% endif %}

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

    {# ── Atlanta Portfolio Analysis section ── #}
    {% if demographics and demographics.atlanta_parcel_count > 0 %}
    <p class="profile-section-label">ATLANTA PORTFOLIO ANALYSIS <span class="src-ref"><a href="/faq/#demographics">*</a></span></p>
    <div class="demographics-grid">
      <div class="demo-card">
        <h3>Neighborhood Income</h3>
        <p>Across their <strong>{{ demographics.atlanta_parcel_count }}</strong> city parcels, the average neighborhood median income is <strong>${{ "{:,.0f}".format(demographics.avg_neighborhood_income) }}</strong>.</p>
        <div class="income-buckets">
          {% for bucket in demographics.income_buckets %}
          <div class="bucket-row">
            <span class="bucket-label">{{ bucket.label }}</span>
            <div class="bucket-bar-bg">
              <div class="bucket-bar" style="width: {{ (bucket.count / demographics.atlanta_parcel_count * 100) | round }}%"></div>
            </div>
            <span class="bucket-count">{{ bucket.count }}</span>
          </div>
          {% endfor %}
        </div>
      </div>
      <div class="demo-card">
        <h3>Tenure & Concentration</h3>
        <p>On average, their Atlanta portfolio is located in neighborhoods where <strong>{{ demographics.avg_neighborhood_renter_pct | round(1) }}%</strong> of households are renters.</p>

        {% set top_ms = demographics.market_share_json.items() | sort(attribute='1.rental_share', reverse=true) | selectattr('1.rental_share', 'gt', 0.5) | list %}
        {% if top_ms %}
        <div class="market-share-box">
          <p class="small">Highest rental market share by neighborhood:</p>
          <ul class="market-share-list">
            {% for nbhd, stats in top_ms[:3] %}
            <li><strong>{{ nbhd }}</strong>: {{ stats.rental_share }}% of all rentals</li>
            {% endfor %}
          </ul>
        </div>
        {% endif %}
      </div>
      <div class="demo-card">
        <h3>Racial Composition</h3>
        {% set black_pct  = demographics.avg_neighborhood_black_pct %}
        {% set white_pct  = demographics.avg_neighborhood_white_pct %}
        {% set hisp_pct   = demographics.avg_neighborhood_hispanic_pct %}
        {% set asian_pct  = demographics.avg_neighborhood_asian_pct %}
        {% set other_pct  = [0, 100 - black_pct - white_pct - hisp_pct - asian_pct] | max %}
        <p>Their portfolio's neighborhoods average <strong>{{ black_pct | round(1) }}%</strong> Black, <strong>{{ white_pct | round(1) }}%</strong> White, <strong>{{ hisp_pct | round(1) }}%</strong> Hispanic, <strong>{{ asian_pct | round(1) }}%</strong> Asian.</p>
        <div class="race-bar">
          {% if black_pct > 0  %}<div class="race-segment" style="width:{{ black_pct | round(1) }}%; background:#6366f1;" title="Black {{ black_pct | round(1) }}%"></div>{% endif %}
          {% if white_pct > 0  %}<div class="race-segment" style="width:{{ white_pct | round(1) }}%; background:#94a3b8;" title="White {{ white_pct | round(1) }}%"></div>{% endif %}
          {% if hisp_pct > 0   %}<div class="race-segment" style="width:{{ hisp_pct | round(1) }}%; background:#f59e0b;" title="Hispanic {{ hisp_pct | round(1) }}%"></div>{% endif %}
          {% if asian_pct > 0  %}<div class="race-segment" style="width:{{ asian_pct | round(1) }}%; background:#10b981;" title="Asian {{ asian_pct | round(1) }}%"></div>{% endif %}
          {% if other_pct > 0  %}<div class="race-segment" style="width:{{ other_pct | round(1) }}%; background:#e2e8f0;" title="Other {{ other_pct | round(1) }}%"></div>{% endif %}
        </div>
        <div class="race-legend">
          {% if black_pct > 0  %}<span class="race-legend-item"><span class="race-dot" style="background:#6366f1"></span>Black</span>{% endif %}
          {% if white_pct > 0  %}<span class="race-legend-item"><span class="race-dot" style="background:#94a3b8"></span>White</span>{% endif %}
          {% if hisp_pct > 0   %}<span class="race-legend-item"><span class="race-dot" style="background:#f59e0b"></span>Hispanic</span>{% endif %}
          {% if asian_pct > 0  %}<span class="race-legend-item"><span class="race-dot" style="background:#10b981"></span>Asian</span>{% endif %}
          {% if other_pct > 0  %}<span class="race-legend-item"><span class="race-dot" style="background:#e2e8f0"></span>Other</span>{% endif %}
        </div>
      </div>
      <div class="demo-card">
        <h3>Home Values &amp; Vulnerability</h3>
        <p>Avg neighborhood median home value is <strong>${{ "{:,.0f}".format(demographics.avg_neighborhood_home_value) }}</strong>.</p>
        <div class="income-buckets">
          {% set hv_total = demographics.home_value_buckets | sum(attribute='count') %}
          {% for bucket in demographics.home_value_buckets %}
          <div class="bucket-row">
            <span class="bucket-label">{{ bucket.label }}</span>
            <div class="bucket-bar-bg">
              <div class="bucket-bar" style="width: {{ (bucket.count / hv_total * 100) | round if hv_total > 0 else 0 }}%"></div>
            </div>
            <span class="bucket-count">{{ bucket.count }}</span>
          </div>
          {% endfor %}
        </div>
        <div class="vuln-stats">
          <div class="vuln-stat">
            <strong>{{ demographics.avg_neighborhood_poverty_pct | round(1) }}%</strong>
            households below poverty
          </div>
          <div class="vuln-stat">
            <strong>{{ demographics.avg_neighborhood_vacant_pct | round(1) }}%</strong>
            housing units vacant
          </div>
        </div>
      </div>
    </div>

    {% if demographics.atlanta_parcel_count >= 10 %}
    <div class="portfolio-maps">
      <div class="map-container">
        <h4>Portfolio vs. Neighborhood Income</h4>
        <a href="/img/owners/cluster_{{ cluster_id }}_income.png" target="_blank">
          <img src="/img/owners/cluster_{{ cluster_id }}_income.png" alt="Map showing portfolio on income choropleth" loading="lazy" onerror="this.style.display='none'">
        </a>
      </div>
      <div class="map-container">
        <h4>Portfolio vs. Renter Concentration</h4>
        <a href="/img/owners/cluster_{{ cluster_id }}_renter.png" target="_blank">
          <img src="/img/owners/cluster_{{ cluster_id }}_renter.png" alt="Map showing portfolio on renter choropleth" loading="lazy" onerror="this.style.display='none'">
        </a>
      </div>
    </div>
    {% endif %}
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
    <p class="lead">Individual registered agent accounts appearing across multiple owner clusters.
      <span class="muted">{{ total }} accounts shown.</span></p>

    <div class="view-toggle">
      <a href="/l/agents/" class="toggle-btn">Grouped by Name</a>
      <span class="toggle-btn active">All Individual Accounts</span>
    </div>

    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Agent Account</th>
          <th class="num">Clusters</th>
          <th class="num">Parcels</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td>
            <a href="/agent/{{ r.ra_id }}/">{{ r.name | e }}</a>
            <div class="agent-composition-bar" title="Portfolio composition: {{ r.corp_pct }}% Corporate, {{ r.inst_pct }}% Institutional, {{ r.other_pct }}% Other">
              {% if r.corp_pct > 0 %}<span class="seg-corp" style="width: {{ r.corp_pct }}%"></span>{% endif %}
              {% if r.inst_pct > 0 %}<span class="seg-inst" style="width: {{ r.inst_pct }}%"></span>{% endif %}
              {% if r.other_pct > 0 %}<span class="seg-other" style="width: {{ r.other_pct }}%"></span>{% endif %}
            </div>
            {% if r.address %}<div class="ra-address-small">{{ r.address | e }}</div>{% endif %}
          </td>
          <td class="num">{{ r.cluster_count }}</td>
          <td class="num">{{ r.total_parcels }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
""" + _BASE_FOOT

GROUPED_AGENTS_INDEX_TMPL = _BASE_HEAD + """\
    <p class="breadcrumb"><a href="/l/">← Leaderboards</a></p>
    <h1>Registered Agents</h1>
    <p class="lead">Registered agents appearing across multiple owner clusters, grouped by name.
      <span class="muted">{{ total }} names shown.</span></p>

    <div class="view-toggle">
      <span class="toggle-btn active">Grouped by Name</span>
      <a href="/l/agents/all/" class="toggle-btn">All Individual Accounts</a>
    </div>

    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Agent Name</th>
          <th class="num">Accounts</th>
          <th class="num">Clusters</th>
          <th class="num">Parcels</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td>
            <a href="/agent/by-name/{{ r.slug }}/">{{ r.name | e }}</a>
            <div class="agent-composition-bar" title="Portfolio composition: {{ r.corp_pct }}% Corporate, {{ r.inst_pct }}% Institutional, {{ r.other_pct }}% Other">
              {% if r.corp_pct > 0 %}<span class="seg-corp" style="width: {{ r.corp_pct }}%"></span>{% endif %}
              {% if r.inst_pct > 0 %}<span class="seg-inst" style="width: {{ r.inst_pct }}%"></span>{% endif %}
              {% if r.other_pct > 0 %}<span class="seg-other" style="width: {{ r.other_pct }}%"></span>{% endif %}
            </div>
          </td>
          <td class="num">{{ r.account_count }}</td>
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
    <p class="breadcrumb"><a href="/l/">← Leaderboards</a> / <a href="/l/agents/all/">Registered Agents</a></p>
    <div class="owner-header">
      <div class="owner-names">
        <h1>{{ agent_name | e }}</h1>
        {% if agent_address %}<p class="agent-address-sub">{{ agent_address | e }}</p>{% endif %}
      </div>
    </div>

    {% if account_count > 1 %}
    <div class="agent-group-link">
      <p>This is one of <strong>{{ account_count }}</strong> individual accounts registered under the name 
         <a href="/agent/by-name/{{ name_slug }}/"><strong>{{ agent_name | e }}</strong></a>.</p>
    </div>
    {% endif %}

    <p class="lead">Registered agent for {{ cluster_count }} owner cluster{{ 's' if cluster_count != 1 else '' }}
      ({{ total_parcels }} parcel{{ 's' if total_parcels != 1 else '' }} total)</p>

    <div class="table-scroll">
    <table class="leaderboard-table">
      <thead>
        <tr>
          <th>Owner</th>
          <th class="num">Parcels <span class="cap-note" style="text-transform:none; font-weight:400; opacity:0.7">(City / Total)</span></th>
        </tr>
      </thead>
      <tbody>
        {% for row in clusters %}
        <tr class="owner-main-row">
          <td class="owner-cell">
            <div class="owner-name-row">
              <a href="/owner/{{ row.cluster_id }}/">{{ row.primary_name | e }}</a>
              {% if row.income_spark %}
              <span class="income-spark" title="Atlanta portfolio: income distribution Low→High">{% for seg in row.income_spark %}<span style="width:{{ seg.pct }}%;background:{{ seg.color }}"></span>{% endfor %}</span>
              {% endif %}
            </div>
          </td>
          <td class="num">
            <span class="city-count" style="font-weight:600">{{ row.atlanta_parcel_count }}</span>
            <span class="count-separator" style="opacity:0.4; margin:0 2px">/</span>
            <span class="total-count" style="opacity:0.8">{{ row.parcel_count }}</span>
          </td>
        </tr>
        {% if row.is_corporate or row.is_institutional or row.foreign_states %}
        <tr class="owner-badges-row-tr">
          <td colspan="2">
            <div class="leaderboard-badges-row">
              {% if row.is_corporate %}<span class="badge-corporate">CORPORATE</span>{% endif %}
              {% if row.is_institutional %}<span class="badge-institutional">INSTITUTIONAL</span>{% endif %}
              {% if row.foreign_states %}
                {% for st in row.foreign_states %}
                <span class="badge-state">{{ st | upper | e }}</span>
                {% endfor %}
              {% endif %}
            </div>
          </td>
        </tr>
        {% endif %}
        {% endfor %}
      </tbody>
    </table>
    </div>

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
""" + _BASE_FOOT

GROUPED_AGENT_TMPL = _BASE_HEAD + """\
    <p class="breadcrumb"><a href="/l/">← Leaderboards</a> / <a href="/l/agents/">Registered Agents</a></p>
    <div class="owner-header">
      <div class="owner-names">
        <h1>{{ agent_name | e }}</h1>
      </div>
    </div>

    <p class="lead">Registered agent for <strong>{{ cluster_count }}</strong> owner cluster{{ 's' if cluster_count != 1 else '' }} 
      across <strong>{{ account_count }}</strong> individual GA SOS accounts.
      ({{ total_parcels }} parcel{{ 's' if total_parcels != 1 else '' }} total)</p>

    <div class="table-scroll">
    <table class="leaderboard-table">
      <thead>
        <tr>
          <th>Owner</th>
          <th class="num">Parcels <span class="cap-note" style="text-transform:none; font-weight:400; opacity:0.7">(City / Total)</span></th>
        </tr>
      </thead>
      <tbody>
        {% for row in clusters %}
        <tr class="owner-main-row">
          <td class="owner-cell">
            <div class="owner-name-row">
              <a href="/owner/{{ row.cluster_id }}/">{{ row.primary_name | e }}</a>
              {% if row.income_spark %}
              <span class="income-spark" title="Atlanta portfolio: income distribution Low→High">{% for seg in row.income_spark %}<span style="width:{{ seg.pct }}%;background:{{ seg.color }}"></span>{% endfor %}</span>
              {% endif %}
            </div>
          </td>
          <td class="num">
            <span class="city-count" style="font-weight:600">{{ row.atlanta_parcel_count }}</span>
            <span class="count-separator" style="opacity:0.4; margin:0 2px">/</span>
            <span class="total-count" style="opacity:0.8">{{ row.parcel_count }}</span>
          </td>
        </tr>
        {% if row.is_corporate or row.is_institutional or row.foreign_states %}
        <tr class="owner-badges-row-tr">
          <td colspan="2">
            <div class="leaderboard-badges-row">
              {% if row.is_corporate %}<span class="badge-corporate">CORPORATE</span>{% endif %}
              {% if row.is_institutional %}<span class="badge-institutional">INSTITUTIONAL</span>{% endif %}
              {% if row.foreign_states %}
                {% for st in row.foreign_states %}
                <span class="badge-state">{{ st | upper | e }}</span>
                {% endfor %}
              {% endif %}
            </div>
          </td>
        </tr>
        {% endif %}
        {% endfor %}
      </tbody>
    </table>
    </div>

    <div class="accounts-section" style="margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #eee;">
      <h3>Individual Agent Accounts</h3>
      <p class="small muted">GA SOS assigns unique IDs to each registered agent entry. These accounts all share this name:</p>
      <ul class="ra-accounts-list">
        {% for acc in accounts %}
        <li>
          <a href="/agent/{{ acc.ra_id }}/">{{ acc.name | e }}</a>
          {% if acc.address %} — <span class="muted">{{ acc.address | e }}</span>{% endif %}
          <span class="small muted">({{ acc.cluster_count }} clusters)</span>
        </li>
        {% endfor %}
      </ul>
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
          <td>
            <a href="{{ r.url }}">{{ r.area | e }}</a>
            {% if r.area_spark %}
            <span class="income-spark" title="Area-wide: income distribution Low→High">{% for seg in r.area_spark %}<span style="width:{{ seg.pct }}%;background:{{ seg.color }}"></span>{% endfor %}</span>
            {% endif %}
          </td>
          <td class="num">{{ r.total_parcels }}</td>
          <td class="muted">
            {{ r.top_owner | e }}
            {% if r.top_owner_spark %}
            <span class="income-spark" title="Top owner Atlanta portfolio: income distribution Low→High">{% for seg in r.top_owner_spark %}<span style="width:{{ seg.pct }}%;background:{{ seg.color }}"></span>{% endfor %}</span>
            {% endif %}
          </td>
          {% if r.map_url %}<td class="map-link-cell"><a href="{{ r.map_url }}" title="View on map" class="map-link-small">map →</a></td>{% endif %}
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
""" + _BASE_FOOT

ATL_ZONING_INDEX_TMPL = _BASE_HEAD + """\
    <p class="breadcrumb"><a href="/l/">← Leaderboards</a></p>
    <h1>Atlanta Zoning &amp; Home Types</h1>
    <p class="lead">Top property owners by Atlanta residential zoning district and home type.</p>

    <h2>By Home Type</h2>
    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Home Type</th>
          <th class="num">Parcels</th>
          <th>Top owner</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for r in ht_rows %}
        <tr>
          <td><a href="{{ r.url }}">{{ r.area | e }}</a></td>
          <td class="num">{{ r.total_parcels }}</td>
          <td class="muted">{{ r.top_owner | e }}</td>
          <td class="map-link-cell"><a href="{{ r.map_url }}" title="View on map" class="map-link-small">map →</a></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <h2>By Zoning District</h2>
    <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Zoning District</th>
          <th class="num">Parcels</th>
          <th>Top owner</th>
        </tr>
      </thead>
      <tbody>
        {% for r in zoning_rows %}
        <tr>
          <td><a href="{{ r.url }}">{{ r.area | e }}</a></td>
          <td class="num">{{ r.total_parcels }}</td>
          <td class="muted">{{ r.top_owner | e }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>

    <p class="sources-footnote"><a href="/faq/#data-sources">ⓘ Data sources</a></p>
""" + _BASE_FOOT

GEO_LEADERBOARD_TMPL = _BASE_HEAD + """\
    <p class="breadcrumb"><a href="{{ index_url }}">← {{ index_label }}</a></p>
    
    {% if area_stats and geo_key == 'neighborhood' %}
    <div class="demo-card" style="margin-bottom: 1.5rem; padding: 0.75rem 1rem;">
      <div style="display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 1rem; margin-bottom: 0.5rem;">
        <h2 style="margin: 0; font-size: 1rem; color: var(--pico-muted-color); text-transform: uppercase; letter-spacing: 0.05em;">{{ area_name | e }} Stats</h2>
        <span style="font-size: 0.8rem; color: var(--pico-muted-color);">{% if area_stats.total_population %}Pop. {{ area_stats.total_population | format_int }}{% else %}Population N/A{% endif %}</span>
      </div>
      
      <div class="vuln-stats" style="margin-top: 0; padding-top: 0; border-top: none; gap: 1.5rem; flex-wrap: wrap;">
        <div class="vuln-stat">
          <strong>${{ (area_stats.median_income or 0) | int | format_int }}</strong>
          <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--pico-muted-color);">Median Income</span>
        </div>
        <div class="vuln-stat">
          <strong>{{ (area_stats.renter_pct or 0) | round(1) }}%</strong>
          <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--pico-muted-color);">Renters</span>
        </div>
        <div class="vuln-stat">
          <strong>${{ (area_stats.median_home_value or 0) | int | format_int }}</strong>
          <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--pico-muted-color);">Home Value</span>
        </div>
        <div class="vuln-stat">
          <strong>{{ (area_stats.vacant_pct or 0) | round(1) }}%</strong>
          <span style="font-size: 0.7rem; text-transform: uppercase; color: var(--pico-muted-color);">Vacancy</span>
        </div>
      </div>

      <div style="margin-top: 0.75rem;">
        <div class="race-bar" style="height: 6px; margin: 0.25rem 0 0.5rem;">
          {% if (area_stats.black_pct or 0) > 0    %}<div class="race-segment" style="width:{{ (area_stats.black_pct or 0) | round(1) }}%;    background:#6366f1;" title="Black {{ (area_stats.black_pct or 0) | round(1) }}%"></div>{% endif %}
          {% if (area_stats.white_pct or 0) > 0    %}<div class="race-segment" style="width:{{ (area_stats.white_pct or 0) | round(1) }}%;    background:#94a3b8;" title="White {{ (area_stats.white_pct or 0) | round(1) }}%"></div>{% endif %}
          {% if (area_stats.hispanic_pct or 0) > 0 %}<div class="race-segment" style="width:{{ (area_stats.hispanic_pct or 0) | round(1) }}%; background:#f59e0b;" title="Hispanic {{ (area_stats.hispanic_pct or 0) | round(1) }}%"></div>{% endif %}
          {% if (area_stats.asian_pct or 0) > 0    %}<div class="race-segment" style="width:{{ (area_stats.asian_pct or 0) | round(1) }}%;    background:#10b981;" title="Asian {{ (area_stats.asian_pct or 0) | round(1) }}%"></div>{% endif %}
          {% if (area_stats.other_pct or 0) > 0    %}<div class="race-segment" style="width:{{ (area_stats.other_pct or 0) | round(1) }}%;    background:#e2e8f0;" title="Other {{ (area_stats.other_pct or 0) | round(1) }}%"></div>{% endif %}
        </div>
        <div class="race-legend" style="font-size: 0.65rem; gap: 0.4rem 0.75rem;">
          {% if (area_stats.black_pct or 0) > 0    %}<span class="race-legend-item"><span class="race-dot" style="background:#6366f1"></span>{{ (area_stats.black_pct or 0) | round(1) }}% Black</span>{% endif %}
          {% if (area_stats.white_pct or 0) > 0    %}<span class="race-legend-item"><span class="race-dot" style="background:#94a3b8"></span>{{ (area_stats.white_pct or 0) | round(1) }}% White</span>{% endif %}
          {% if (area_stats.hispanic_pct or 0) > 0 %}<span class="race-legend-item"><span class="race-dot" style="background:#f59e0b"></span>{{ (area_stats.hispanic_pct or 0) | round(1) }}% Hisp</span>{% endif %}
          {% if (area_stats.asian_pct or 0) > 0    %}<span class="race-legend-item"><span class="race-dot" style="background:#10b981"></span>{{ (area_stats.asian_pct or 0) | round(1) }}% Asian</span>{% endif %}
          {% if (area_stats.other_pct or 0) > 0    %}<span class="race-legend-item"><span class="race-dot" style="background:#e2e8f0"></span>{{ (area_stats.other_pct or 0) | round(1) }}% Other</span>{% endif %}
        </div>
      </div>
    </div>
    {% endif %}

    <div class="geo-title-row">
      <div class="geo-title-name">
        <h1>{{ area_name | e }}</h1>
        {% if area_spark %}
        <span class="income-spark" title="Area-wide: income distribution Low→High">{% for seg in area_spark %}<span style="width:{{ seg.pct }}%;background:{{ seg.color }}"></span>{% endfor %}</span>
        {% endif %}
      </div>
      {% if area_map_url %}<a href="{{ area_map_url }}" class="geo-map-link">view on map →</a>{% endif %}
    </div>

    {% if sub_filters %}
    <nav class="sub-filter-nav">
      {% for f in sub_filters %}
      {% if f.active %}
      <span class="sub-filter-current">{{ f.label | e }}</span>
      {% else %}
      <a href="{{ f.url }}">{{ f.label | e }}</a>
      {% endif %}
      {% endfor %}
    </nav>
    {% endif %}

    <p class="lead">Top property owners within this {{ geo_type_label }}.
      <span class="muted">{{ total }} owners shown, {{ area_total_parcels }} total parcels.</span></p>

    <div class="table-scroll">
    <table class="leaderboard-table">
      <thead>
        <tr>
          <th>#</th>
          <th>Owner</th>
          <th class="num">In area</th>
          <th class="num">Total</th>
          {% if geo_key %}<th></th>{% endif %}
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr class="owner-main-row">
          <td class="rank">{{ loop.index }}</td>
          <td class="owner-cell">
            <div class="owner-name-row">
              <a href="/owner/{{ r.cluster_id }}/">{{ r.primary_name | e }}</a>
            </div>
            {% if r.alt_names %}
            <div class="alt-names">{{ r.alt_names | e }}</div>
            {% endif %}
          </td>
          <td class="num">{{ r.local_parcel_count }}</td>
          <td class="num muted">{{ r.total_parcel_count }}</td>
          {% if geo_key %}
          <td class="map-link-cell"><a href="/?cluster={{ r.cluster_id }}&geo={{ geo_key }}&area={{ area_raw_enc }}" title="View on map" class="map-link">map →</a></td>
          {% endif %}
        </tr>
        {% if r.is_corporate or r.is_institutional or r.foreign_states %}
        <tr class="owner-badges-row-tr">
          <td></td>
          <td colspan="{{ '4' if geo_key else '3' }}">
            <div class="leaderboard-badges-row">
              {% if r.is_corporate %}<span class="badge-corporate">CORPORATE</span>{% endif %}
              {% if r.is_institutional %}<span class="badge-institutional">INSTITUTIONAL</span>{% endif %}
              {% if r.foreign_states %}
                {% for st in r.foreign_states %}
                <span class="badge-state">{{ st | upper | e }}</span>
                {% endfor %}
              {% endif %}
            </div>
          </td>
        </tr>
        {% endif %}
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

# Income bucket order and colors for the inline spark bar
_SPARK_BUCKETS = [
    ("Low",      "#ef4444"),  # red
    ("Low-Mid",  "#f97316"),  # orange
    ("Mid",      "#eab308"),  # yellow
    ("Mid-High", "#84cc16"),  # lime
    ("High",     "#22c55e"),  # green
]

def _income_spark(bucket_counts):
    """Return list of (color, width_pct) for the income spark bar, or None if no data."""
    if not bucket_counts:
        return None
    total = sum(bucket_counts.get(b, 0) for b, _ in _SPARK_BUCKETS)
    if total == 0:
        return None
    return [
        {"color": color, "pct": round(bucket_counts.get(bucket, 0) / total * 100, 1)}
        for bucket, color in _SPARK_BUCKETS
    ]


def fetch_ownership_demographics(conn):
    """Fetch data from the two ownership demographics MVs."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM mv_ownership_demographics ORDER BY parcel_count DESC")
        _type_order = {"corporate": 0, "institutional": 1, "individual": 2}
        by_type = sorted([dict(r) for r in cur.fetchall()], key=lambda r: _type_order.get(r["owner_type"], 99))

        cur.execute("SELECT * FROM mv_ownership_by_income_quartile ORDER BY owner_type, income_quartile")
        quartile_rows = cur.fetchall()

        # New query for intensity (portfolio size)
        cur.execute("""
            SELECT
                CASE
                    WHEN institutional_parcel_count > 0 THEN 'institutional'
                    WHEN corporate_parcel_count > 0 THEN 'corporate'
                    ELSE 'individual'
                END AS owner_type,
                round(avg(parcel_count), 1) AS avg_portfolio_size
            FROM mv_cluster_stats
            GROUP BY 1
        """)
        intensity_rows = {r['owner_type']: r['avg_portfolio_size'] for r in cur.fetchall()}
        
        # New query for out-of-state %
        cur.execute("""
            WITH cluster_counts AS (
                SELECT
                    CASE
                        WHEN institutional_parcel_count > 0 THEN 'institutional'
                        WHEN corporate_parcel_count > 0 THEN 'corporate'
                        ELSE 'individual'
                    END AS owner_type,
                    atlanta_parcel_count,
                    primary_foreign_state
                FROM mv_cluster_stats
                WHERE atlanta_parcel_count > 0
            )
            SELECT
                owner_type,
                round(100.0 * sum(CASE WHEN primary_foreign_state IS NOT NULL AND primary_foreign_state NOT IN ('Georgia', 'GA') THEN atlanta_parcel_count ELSE 0 END) / sum(atlanta_parcel_count), 1) AS pct_out_of_state
            FROM cluster_counts
            GROUP BY 1
        """)
        oos_rows = {r['owner_type']: r['pct_out_of_state'] for r in cur.fetchall()}

        # New query for top corporate neighborhoods (min 50 parcels)
        cur.execute("""
            WITH totals AS (
                SELECT city_neighborhood, count(*) AS total_parcels
                FROM parcels_unified WHERE city_neighborhood IS NOT NULL GROUP BY 1
                HAVING count(*) >= 50
            ),
            corporate AS (
                SELECT city_neighborhood, count(*) AS corporate_parcels
                FROM parcels_unified WHERE city_neighborhood IS NOT NULL AND is_corporate = TRUE GROUP BY 1
            )
            SELECT
                t.city_neighborhood AS name, 
                t.total_parcels, 
                c.corporate_parcels,
                round(100.0 * c.corporate_parcels / t.total_parcels, 1) AS pct_corporate
            FROM totals t JOIN corporate c ON t.city_neighborhood = c.city_neighborhood
            ORDER BY pct_corporate DESC LIMIT 5
        """)
        top_neighborhoods = [dict(r) for r in cur.fetchall()]
        for n in top_neighborhoods:
            n['slug'] = slugify(n['name'])
            n['name_enc'] = quote_plus(n['name'])

    # Merge intensity and OOS into by_type
    for r in by_type:
        r['avg_portfolio_size'] = intensity_rows.get(r['owner_type'], 0)
        r['pct_out_of_state'] = oos_rows.get(r['owner_type'], 0)

    # Add ranking classes for each metric
    metrics = [
        "parcel_count", "avg_portfolio_size", "pct_out_of_state",
        "median_neighborhood_income", "avg_neighborhood_income", "avg_neighborhood_home_value", 
        "avg_bachelors_pct", "avg_renter_pct", "avg_poverty_pct", "avg_vacant_pct"
    ]
    for m in metrics:
        vals = [r.get(m, 0) for r in by_type]
        sorted_vals = sorted(list(set(vals)))  # unique values sorted ascending
        for r in by_type:
            val = r.get(m, 0)
            if not sorted_vals:
                r[f"{m}_class"] = "cell-low"
            elif len(sorted_vals) == 1:
                r[f"{m}_class"] = "cell-med"
            elif val == sorted_vals[-1]:
                r[f"{m}_class"] = "cell-high"
            elif val == sorted_vals[0]:
                r[f"{m}_class"] = "cell-low"
            else:
                r[f"{m}_class"] = "cell-med"

    # Organise quartile rows by owner_type
    quartile_data = {}
    for row in quartile_rows:
        t = row["owner_type"]
        quartile_data.setdefault(t, []).append(dict(row))

    # Pre-compute totals for the lede paragraph
    corp  = next((r for r in by_type if r["owner_type"] == "corporate"), {})
    indiv = next((r for r in by_type if r["owner_type"] == "individual"), {})
    total_parcels = sum(r["parcel_count"] for r in by_type)
    # Q1+Q2 concentration percentages
    corp_q12    = sum(r["parcel_count"] for r in quartile_data.get("corporate",  []) if r["income_quartile"] in (1, 2))
    indiv_q12   = sum(r["parcel_count"] for r in quartile_data.get("individual", []) if r["income_quartile"] in (1, 2))
    corp_total  = sum(r["parcel_count"] for r in quartile_data.get("corporate",  []))
    indiv_total = sum(r["parcel_count"] for r in quartile_data.get("individual", []))
    totals = {
        "total_parcels":  total_parcels,
        "corp_income":    corp.get("avg_neighborhood_income", 0),
        "indiv_income":   indiv.get("avg_neighborhood_income", 0),
        "corp_black":     corp.get("avg_black_pct", 0),
        "indiv_black":    indiv.get("avg_black_pct", 0),
        "corp_q12_pct":   round(corp_q12  / corp_total  * 100, 1) if corp_total  else 0,
        "indiv_q12_pct":  round(indiv_q12 / indiv_total * 100, 1) if indiv_total else 0,
    }
    return by_type, quartile_data, totals, top_neighborhoods


def render_numbers_page(by_type, quartile_data, totals, top_neighborhoods, last_updated_str):
    env = _make_env()
    tmpl = env.from_string(NUMBERS_TMPL)
    return tmpl.render(
        by_type=by_type,
        quartile_data=quartile_data,
        totals=totals,
        top_neighborhoods=top_neighborhoods,
        last_updated_str=last_updated_str,
    )


def build_numbers_page(conn, output_dir, last_updated_str=None):
    if last_updated_str is None:
        last_updated_str = fetch_last_update(conn)
    print("Building /numbers/ page...", end=" ", flush=True)
    by_type, quartile_data, totals, top_neighborhoods = fetch_ownership_demographics(conn)
    html = render_numbers_page(by_type, quartile_data, totals, top_neighborhoods, last_updated_str)
    out = Path(output_dir) / "numbers" / "index.html"
    write_if_changed(out, html)
    print(f"done → {out}")


def fetch_last_update(conn):
    """Returns the latest last_updated timestamp as a formatted string."""
    with conn.cursor() as cur:
        # Check portfolio_demographics first as it's the latest in the pipeline
        cur.execute("SELECT MAX(last_updated) FROM portfolio_demographics")
        dt = cur.fetchone()[0]
        if dt:
            return dt.strftime("%Y-%m-%d %H:%M")
        return time.strftime("%Y-%m-%d %H:%M")

def render_leaderboard(rows, last_updated_str):
    env = _make_env()
    tmpl = env.from_string(LEADERBOARD_TMPL)
    return tmpl.render(
        page_title="Top Landlords",
        meta_description="The top corporate and institutional property owners in Atlanta, ranked by parcel count across Fulton and DeKalb counties.",
        rows=rows,
        total=len(rows),
        last_updated_str=last_updated_str,
    )

def render_owner(cluster_id, stats, parcels, county_breakdown, sos_data, neighborhoods,
                 linkable_agent_ids=frozenset(), cluster_related=None,
                 entity_sos_ids=None, officers=None, demographics=None, 
                 last_updated_str=None):
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

    # Badges for the horizontal row below stats
    badges = []
    if bool(stats["corporate_parcel_count"]):
        badges.append({"label": "CORPORATE", "class": "badge-corporate"})
    if bool(stats["institutional_parcel_count"]):
        badges.append({"label": "INSTITUTIONAL", "class": "badge-institutional"})
    for st in sos_states:
        if st and st.strip().upper() not in ("GEORGIA", "GA"):
            badges.append({"label": st.upper(), "class": "badge-state"})

    # Demographics
    if demographics:
        # Convert numeric types for template
        for key in ("avg_neighborhood_income", "avg_neighborhood_renter_pct",
                    "avg_neighborhood_white_pct", "avg_neighborhood_black_pct",
                    "avg_neighborhood_hispanic_pct", "avg_neighborhood_asian_pct",
                    "avg_neighborhood_poverty_pct", "avg_neighborhood_home_value",
                    "avg_neighborhood_vacant_pct"):
            demographics[key] = float(demographics.get(key) or 0)

        # Sort income buckets for display
        buckets_order = ['Low', 'Low-Mid', 'Mid', 'Mid-High', 'High']
        raw_buckets = demographics.get("income_bucket_counts") or {}
        sorted_buckets = []
        for b in buckets_order:
            if b in raw_buckets:
                sorted_buckets.append({"label": b, "count": raw_buckets[b]})
        demographics["income_buckets"] = sorted_buckets

        # Sort home value buckets for display
        hv_order = ['<$150k', '$150-300k', '$300-500k', '$500k+']
        raw_hv = demographics.get("home_value_bucket_counts") or {}
        sorted_hv = []
        for b in hv_order:
            if b in raw_hv:
                sorted_hv.append({"label": b, "count": raw_hv[b]})
        demographics["home_value_buckets"] = sorted_hv

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
        demographics=demographics,
        badges=badges,
        last_updated_str=last_updated_str,
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
            SELECT l.cluster_id, l.owner_names, l.parcel_count, l.atlanta_parcel_count,
                   l.total_land_acres, l.corporate_parcel_count, l.institutional_parcel_count,
                   l.primary_sos_status, l.primary_foreign_state, l.foreign_states,
                   pd.income_bucket_counts
            FROM mv_leaderboard l
            LEFT JOIN portfolio_demographics pd ON l.cluster_id = pd.cluster_id
            ORDER BY l.parcel_count DESC
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
    """Returns {ra_id: {name, address, cluster_count}} for individual (non-commercial) RAs in ≥2 clusters."""
    blocklist_clauses = " AND ".join(
        f"oe.sos_registered_agent NOT ILIKE %s" for _ in COMMERCIAL_RA_PATTERNS
    )
    sql = f"""
        SELECT oe.sos_registered_agent_id AS ra_id,
               MAX(oe.sos_registered_agent) AS ra_name,
               MAX(oe.sos_registered_agent_address) AS ra_address,
               COUNT(DISTINCT oe.cluster_id) AS cluster_count
        FROM owner_entities oe
        JOIN ownership_clusters oc USING (cluster_id)
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
            ra_id, ra_name, ra_address, cluster_count = row
            if not is_commercial_ra(ra_name):
                result[ra_id] = {
                    "name": ra_name,
                    "address": ra_address,
                    "cluster_count": int(cluster_count)
                }
        return result


def fetch_agent_clusters(conn, ra_ids):
    """Returns {ra_id: [{cluster_id, primary_name, parcel_count, atlanta_parcel_count, is_corporate, is_institutional, income_spark}, ...]}."""
    if not ra_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT oe.sos_registered_agent_id AS ra_id,
                   oc.cluster_id, oc.owner_names[1] AS primary_name, oc.parcel_count,
                   mc.corporate_parcel_count,
                   mc.institutional_parcel_count,
                   mc.atlanta_parcel_count,
                   pd.income_bucket_counts
            FROM owner_entities oe
            JOIN ownership_clusters oc ON oc.cluster_id = oe.cluster_id
            JOIN mv_cluster_stats mc ON mc.cluster_id = oe.cluster_id
            LEFT JOIN portfolio_demographics pd ON pd.cluster_id = oe.cluster_id
            WHERE oe.sos_registered_agent_id = ANY(%s)
            GROUP BY oe.sos_registered_agent_id, oc.cluster_id, oc.owner_names[1], oc.parcel_count,
                     mc.corporate_parcel_count, mc.institutional_parcel_count,
                     mc.atlanta_parcel_count, pd.income_bucket_counts
            ORDER BY oe.sos_registered_agent_id, oc.parcel_count DESC
        """, (list(ra_ids),))
        result = defaultdict(list)
        for row in cur.fetchall():
            ra_id, cluster_id, primary_name, parcel_count, corp_count, inst_count, atl_count, buckets = row
            result[ra_id].append({
                "cluster_id": cluster_id,
                "primary_name": primary_name or f"Cluster {cluster_id}",
                "parcel_count": int(parcel_count),
                "atlanta_parcel_count": int(atl_count or 0),
                "is_corporate": bool(corp_count and corp_count > 0),
                "is_institutional": bool(inst_count and inst_count > 0),
                "corporate_parcel_count": int(corp_count or 0),
                "institutional_parcel_count": int(inst_count or 0),
                "income_spark": _income_spark(buckets),
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
    """Top owners by parcel count per geographic area (neighborhood, NPU, council, home_type, etc.).
    Returns:
        by_area: {area_name: [rows]}
        area_demographics: {area_name: income_bucket_counts}
        area_stats: {area_name: {median_income, renter_pct, ...}}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        area_demographics = defaultdict(dict)
        area_stats = {}

        # 1. Fetch Area-wide demographics (household-weighted)
        # ONLY if the column is city_neighborhood, city_npu, or city_council
        if col_name in ('city_neighborhood', 'city_npu', 'city_council'):
            cur.execute(f"""
                SELECT p.area,
                       CASE
                           WHEN d.median_household_income < 40000 THEN 'Low'
                           WHEN d.median_household_income < 57000 THEN 'Low-Mid'
                           WHEN d.median_household_income < 84000 THEN 'Mid'
                           WHEN d.median_household_income < 136000 THEN 'Mid-High'
                           ELSE 'High'
                       END as bucket,
                       SUM(d.total_households) as count
                FROM (
                    SELECT DISTINCT {col_name} AS area, city_neighborhood
                    FROM parcels_unified
                    WHERE {col_name} IS NOT NULL
                ) p
                JOIN gis.neighborhood_demographics d ON p.city_neighborhood = d.neighborhood_name
                GROUP BY 1, 2
            """)
            for row in cur.fetchall():
                area_demographics[row["area"]][row["bucket"]] = row["count"]

            # 1b. Fetch summary stats
            cur.execute(f"""
                SELECT p.area,
                       round((SUM(d.median_household_income::bigint * d.total_households) / NULLIF(SUM(d.total_households), 0))::numeric) as median_income,
                       round((SUM(d.median_home_value::bigint * d.total_housing_units) / NULLIF(SUM(d.total_housing_units), 0))::numeric) as median_home_value,
                       round((100.0 * SUM(d.renter_occupied_count) / NULLIF(SUM(d.total_households), 0))::numeric, 1) as renter_pct,
                       round((100.0 * SUM(d.white_pct * d.total_population / 100.0) / NULLIF(SUM(d.total_population), 0))::numeric, 1) as white_pct,
                       round((100.0 * SUM(d.black_pct * d.total_population / 100.0) / NULLIF(SUM(d.total_population), 0))::numeric, 1) as black_pct,
                       round((100.0 * SUM(d.hispanic_pct * d.total_population / 100.0) / NULLIF(SUM(d.total_population), 0))::numeric, 1) as hispanic_pct,
                       round((100.0 * SUM(d.asian_pct * d.total_population / 100.0) / NULLIF(SUM(d.total_population), 0))::numeric, 1) as asian_pct,
                       round((100.0 * SUM(GREATEST(0, 100 - d.white_pct - d.black_pct - d.hispanic_pct - d.asian_pct) * d.total_population / 100.0) / NULLIF(SUM(d.total_population), 0))::numeric, 1) as other_pct,
                       round((100.0 * SUM(d.vacant_units_count) / NULLIF(SUM(d.total_housing_units), 0))::numeric, 1) as vacant_pct,
                       SUM(d.total_population) as total_population
                FROM (
                    SELECT DISTINCT {col_name} AS area, city_neighborhood
                    FROM parcels_unified
                    WHERE {col_name} IS NOT NULL
                ) p
                JOIN gis.neighborhood_demographics d ON p.city_neighborhood = d.neighborhood_name
                GROUP BY 1
            """)
            area_stats = {row["area"]: dict(row) for row in cur.fetchall()}

        # 2. Fetch Top Owners in area
        cur.execute(f"""
            SELECT p.{col_name} AS area,
                   oe.cluster_id,
                   mc.owner_names[1] AS primary_name,
                   mc.owner_names[2:4] AS alt_names_arr,
                   mc.corporate_parcel_count > 0 AS is_corporate,
                   mc.institutional_parcel_count > 0 AS is_institutional,
                   mc.primary_foreign_state,
                   mc.parcel_count AS total_parcel_count,
                   COUNT(*) AS local_parcel_count
            FROM owner_entities oe
            CROSS JOIN LATERAL unnest(oe.parcel_ids) AS pid
            JOIN parcels_unified p ON p.parcel_id = pid
            JOIN mv_cluster_stats mc ON mc.cluster_id = oe.cluster_id
            WHERE p.{col_name} IS NOT NULL
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
            ORDER BY area, local_parcel_count DESC
        """)
        by_area = defaultdict(list)
        for row in cur.fetchall():
            area, cid, name, alt_arr, is_corp, is_inst, fstate, total_count, local_count = (
                row["area"], row["cluster_id"], row["primary_name"], row["alt_names_arr"],
                row["is_corporate"], row["is_institutional"], row["primary_foreign_state"],
                row["total_parcel_count"], row["local_parcel_count"]
            )
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
                "connection_count": 0,
                "income_spark": None  # Removed per user request for geo leaderboards
            })
        return dict(by_area), dict(area_demographics), area_stats


def fetch_atlanta_zoning_geo_data(conn):
    """Fetch geo data for Atlanta zoning districts, but only residential-related ones."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # We only care about resi-related zoning for this specific report
        # R-1 to R-5, RG (Residential General), MR (Multi-family), PD-H (Planned Development Housing)
        RESI_ZONING_PATTERNS = ['R-%', 'RG%', 'MR%', 'PD-H%']
        
        # 1. Fetch Area-wide demographics
        cur.execute("""
            SELECT p.area,
                   CASE
                       WHEN d.median_household_income < 40000 THEN 'Low'
                       WHEN d.median_household_income < 57000 THEN 'Low-Mid'
                       WHEN d.median_household_income < 84000 THEN 'Mid'
                       WHEN d.median_household_income < 136000 THEN 'Mid-High'
                       ELSE 'High'
                   END as bucket,
                   SUM(d.total_households) as count
            FROM (
                SELECT DISTINCT city_zoning AS area, city_neighborhood
                FROM parcels_unified
                WHERE city_zoning IS NOT NULL
                  AND (city_zoning LIKE ANY(%s))
            ) p
            JOIN gis.neighborhood_demographics d ON p.city_neighborhood = d.neighborhood_name
            GROUP BY 1, 2
        """, (RESI_ZONING_PATTERNS,))
        area_demographics = defaultdict(dict)
        for row in cur.fetchall():
            area_demographics[row["area"]][row["bucket"]] = row["count"]

        # 1b. Fetch summary stats
        cur.execute("""
            SELECT p.area,
                   round((SUM(d.median_household_income::bigint * d.total_households) / NULLIF(SUM(d.total_households), 0))::numeric) as median_income,
                   round((SUM(d.median_home_value::bigint * d.total_housing_units) / NULLIF(SUM(d.total_housing_units), 0))::numeric) as median_home_value,
                   round((100.0 * SUM(d.renter_occupied_count) / NULLIF(SUM(d.total_households), 0))::numeric, 1) as renter_pct,
                   round((100.0 * SUM(d.white_pct * d.total_population / 100.0) / NULLIF(SUM(d.total_population), 0))::numeric, 1) as white_pct,
                   round((100.0 * SUM(d.black_pct * d.total_population / 100.0) / NULLIF(SUM(d.total_population), 0))::numeric, 1) as black_pct,
                   round((100.0 * SUM(d.hispanic_pct * d.total_population / 100.0) / NULLIF(SUM(d.total_population), 0))::numeric, 1) as hispanic_pct,
                   round((100.0 * SUM(d.asian_pct * d.total_population / 100.0) / NULLIF(SUM(d.total_population), 0))::numeric, 1) as asian_pct,
                   round((100.0 * SUM(GREATEST(0, 100 - d.white_pct - d.black_pct - d.hispanic_pct - d.asian_pct) * d.total_population / 100.0) / NULLIF(SUM(d.total_population), 0))::numeric, 1) as other_pct,
                   round((100.0 * SUM(d.vacant_units_count) / NULLIF(SUM(d.total_housing_units), 0))::numeric, 1) as vacant_pct,
                   SUM(d.total_population) as total_population
            FROM (
                SELECT DISTINCT city_zoning AS area, city_neighborhood
                FROM parcels_unified
                WHERE city_zoning IS NOT NULL
                  AND (city_zoning LIKE ANY(%s))
            ) p
            JOIN gis.neighborhood_demographics d ON p.city_neighborhood = d.neighborhood_name
            GROUP BY 1
        """, (RESI_ZONING_PATTERNS,))
        area_stats = {row["area"]: dict(row) for row in cur.fetchall()}

        # 2. Fetch Top Owners
        cur.execute("""
            SELECT p.city_zoning AS area,
                   oe.cluster_id,
                   mc.owner_names[1] AS primary_name,
                   mc.owner_names[2:4] AS alt_names_arr,
                   mc.corporate_parcel_count > 0 AS is_corporate,
                   mc.institutional_parcel_count > 0 AS is_institutional,
                   mc.primary_foreign_state,
                   mc.parcel_count AS total_parcel_count,
                   COUNT(*) AS local_parcel_count
            FROM owner_entities oe
            CROSS JOIN LATERAL unnest(oe.parcel_ids) AS pid
            JOIN parcels_unified p ON p.parcel_id = pid
            JOIN mv_cluster_stats mc ON mc.cluster_id = oe.cluster_id
            WHERE p.city_zoning IS NOT NULL
              AND (p.city_zoning LIKE ANY(%s))
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
            ORDER BY area, local_parcel_count DESC
        """, (RESI_ZONING_PATTERNS,))
        
        by_area = defaultdict(list)
        for row in cur.fetchall():
            area = row["area"]
            alts = [n for n in (row["alt_names_arr"] or []) if n]
            by_area[area].append({
                "cluster_id": row["cluster_id"],
                "primary_name": row["primary_name"] or f"Cluster {row['cluster_id']}",
                "alt_names": ", ".join(alts) if alts else "",
                "is_corporate": bool(row["is_corporate"]),
                "is_institutional": bool(row["is_institutional"]),
                "foreign_state": row["primary_foreign_state"],
                "total_parcel_count": int(row["total_parcel_count"]),
                "local_parcel_count": int(row["local_parcel_count"]),
                "connection_count": 0,
                "income_spark": None
            })
        return dict(by_area), dict(area_demographics), area_stats


def fetch_atl_hometype_geo_data(conn):
    """Top owners by parcel count per home type, restricted to Atlanta city limits."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT p.home_type AS area,
                   oe.cluster_id,
                   mc.owner_names[1] AS primary_name,
                   mc.owner_names[2:4] AS alt_names_arr,
                   mc.corporate_parcel_count > 0 AS is_corporate,
                   mc.institutional_parcel_count > 0 AS is_institutional,
                   mc.primary_foreign_state,
                   mc.parcel_count AS total_parcel_count,
                   COUNT(*) AS local_parcel_count
            FROM owner_entities oe
            CROSS JOIN LATERAL unnest(oe.parcel_ids) AS pid
            JOIN parcels_unified p ON p.parcel_id = pid
            JOIN mv_cluster_stats mc ON mc.cluster_id = oe.cluster_id
            WHERE p.home_type IS NOT NULL
              AND p.city_neighborhood IS NOT NULL
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
            ORDER BY area, local_parcel_count DESC
        """)
        by_area = defaultdict(list)
        for row in cur.fetchall():
            alts = [n for n in (row["alt_names_arr"] or []) if n]
            by_area[row["area"]].append({
                "cluster_id": row["cluster_id"],
                "primary_name": row["primary_name"] or f"Cluster {row['cluster_id']}",
                "alt_names": ", ".join(alts) if alts else "",
                "is_corporate": bool(row["is_corporate"]),
                "is_institutional": bool(row["is_institutional"]),
                "foreign_state": row["primary_foreign_state"],
                "total_parcel_count": int(row["total_parcel_count"]),
                "local_parcel_count": int(row["local_parcel_count"]),
                "connection_count": 0,
                "income_spark": None,
            })
        return dict(by_area)


def fetch_county_geo_data(conn):
    """Top owners by parcel count per county and home type.
    Returns:
        by_county_type: {(county, home_type): [rows]}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # 1. Fetch Top Owners by County and Home Type
        cur.execute("""
            SELECT p.county,
                   p.home_type,
                   oe.cluster_id,
                   mc.owner_names[1] AS primary_name,
                   mc.owner_names[2:4] AS alt_names_arr,
                   mc.corporate_parcel_count > 0 AS is_corporate,
                   mc.institutional_parcel_count > 0 AS is_institutional,
                   mc.primary_foreign_state,
                   mc.parcel_count AS total_parcel_count,
                   COUNT(*) AS local_parcel_count
            FROM owner_entities oe
            CROSS JOIN LATERAL unnest(oe.parcel_ids) AS pid
            JOIN parcels_unified p ON p.parcel_id = pid AND p.county = oe.county
            JOIN mv_cluster_stats mc ON mc.cluster_id = oe.cluster_id
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
            ORDER BY county, home_type, local_parcel_count DESC
        """)
        
        by_county_type = defaultdict(list)
        for row in cur.fetchall():
            key = (row["county"], row["home_type"])
            alts = [n for n in (row["alt_names_arr"] or []) if n]
            by_county_type[key].append({
                "cluster_id": row["cluster_id"],
                "primary_name": row["primary_name"] or f"Cluster {row['cluster_id']}",
                "alt_names": ", ".join(alts) if alts else "",
                "is_corporate": bool(row["is_corporate"]),
                "is_institutional": bool(row["is_institutional"]),
                "foreign_state": row["primary_foreign_state"],
                "total_parcel_count": int(row["total_parcel_count"]),
                "local_parcel_count": int(row["local_parcel_count"]),
                "connection_count": 0,
                "income_spark": None
            })
        return dict(by_county_type)


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
    """Write content to path only if it differs from current content.
    Ensures correct permissions (755 for dirs, 644 for files)."""
    path = Path(path)
    if path.exists():
        try:
            if path.read_text() == content:
                # Ensure permissions are correct even if content matches
                if path.stat().st_mode & 0o777 != 0o644:
                    path.chmod(0o644)
                return False
        except Exception:
            pass

    # Create parent directories with 755
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure all parents in the path have 755 (up to the output-dir)
    # This is a bit defensive but helps if mkdir didn't set them as expected
    p = path.parent
    while p and p.as_posix() != '/':
        try:
            if p.exists() and p.stat().st_mode & 0o777 != 0o755:
                p.chmod(0o755)
        except PermissionError:
            break # Stop if we hit a dir we don't own
        p = p.parent

    path.write_text(content)
    path.chmod(0o644)
    return True

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_leaderboard(conn, output_dir, cluster_connection_count=None, last_updated_str=None):
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
            "atlanta_parcel_count": fmt_int(r["atlanta_parcel_count"]),
            "acres": fmt_acres(r["total_land_acres"]),
            "is_corporate": bool(r["corporate_parcel_count"]),
            "is_institutional": bool(r["institutional_parcel_count"]),
            "foreign_state": r["primary_foreign_state"],
            "foreign_states": r["foreign_states"],
            "connection_count": counts.get(r["cluster_id"], 0),
            "income_spark": _income_spark(r["income_bucket_counts"]),
        })

    html = render_leaderboard(rows, last_updated_str)
    for dest in [output_dir / "l" / "index.html",
                 output_dir / "leaderboard" / "index.html"]:
        write_if_changed(dest, html)
    print(f"done ({len(rows)} rows)")

def build_agent_pages(linkable_agents, agent_clusters, output_dir, last_updated_str=None):
    """Generate /agent/{ra_id}/index.html for each individual account,
    plus /agent/by-name/{slug}/index.html for grouped names,
    plus /l/agents/index.html (grouped) and /l/agents/all/index.html (individual)."""
    env = _make_env()
    tmpl = env.from_string(AGENT_TMPL)
    index_tmpl = env.from_string(AGENTS_INDEX_TMPL)
    grouped_index_tmpl = env.from_string(GROUPED_AGENTS_INDEX_TMPL)
    grouped_detail_tmpl = env.from_string(GROUPED_AGENT_TMPL)
    written = 0

    # 1. Group the data by Slug
    # slug -> {name, slug, accounts: [ra_id], clusters: {cluster_id: cluster_info}, is_corporate, is_institutional}
    grouped_map = defaultdict(lambda: {
        "name": "", "slug": "", "accounts": [], "clusters": {},
        "is_corporate": False, "is_institutional": False,
        "corporate_parcel_count": 0, "institutional_parcel_count": 0
    })
    for ra_id, info in linkable_agents.items():
        raw_name = info["name"]
        slug = slugify(raw_name)
        if not slug: continue
        g = grouped_map[slug]
        if not g["name"]:
            g["name"] = raw_name
            g["slug"] = slug
        
        clusters = agent_clusters.get(ra_id, [])
        g["accounts"].append({
            "ra_id": ra_id,
            "name": raw_name,
            "address": info.get("address"),
            "cluster_count": len(clusters)
        })
        for c in clusters:
            cid = c["cluster_id"]
            if cid not in g["clusters"]:
                g["clusters"][cid] = c
                if c.get("is_corporate"): 
                    g["is_corporate"] = True
                    g["corporate_parcel_count"] += c.get("corporate_parcel_count", 0)
                if c.get("is_institutional"): 
                    g["is_institutional"] = True
                    g["institutional_parcel_count"] += c.get("institutional_parcel_count", 0)
            elif c["parcel_count"] > g["clusters"][cid]["parcel_count"]:
                # If we've seen this cluster before via a different RA entry,
                # we don't double-count it, but we keep the most complete info.
                g["clusters"][cid] = c

    # 2. Build Individual Agent Detail Pages
    index_rows = []
    for ra_id, info in linkable_agents.items():
        slug = slugify(info["name"])
        if not slug: continue
        g = grouped_map[slug]
        clusters = agent_clusters.get(ra_id, [])
        total_parcels = sum(c["parcel_count"] for c in clusters)
        
        corp_p = sum(c.get("corporate_parcel_count", 0) for c in clusters)
        inst_p = sum(c.get("institutional_parcel_count", 0) for c in clusters)
        other_p = max(0, total_parcels - corp_p - inst_p)

        html = tmpl.render(
            page_title=f"{info['name']} — Registered Agent",
            meta_description=f"{info['name']} is a registered agent for {info['cluster_count']} owner clusters in Atlanta.",
            agent_name=info["name"],
            agent_address=info.get("address"),
            cluster_count=len(clusters),
            total_parcels=total_parcels,
            clusters=clusters,
            account_count=len(g["accounts"]),
            name_slug=g["slug"],
            last_updated_str=last_updated_str,
        )
        out_path = output_dir / "agent" / str(ra_id) / "index.html"
        write_if_changed(out_path, html)
        written += 1
        index_rows.append({
            "ra_id": ra_id,
            "name": info["name"],
            "address": info.get("address"),
            "cluster_count": len(clusters),
            "total_parcels": total_parcels,
            "is_corporate": corp_p > 0,
            "is_institutional": inst_p > 0,
            "corporate_parcel_count": corp_p,
            "institutional_parcel_count": inst_p,
            "corp_pct": round(corp_p / total_parcels * 100, 1) if total_parcels > 0 else 0,
            "inst_pct": round(inst_p / total_parcels * 100, 1) if total_parcels > 0 else 0,
            "other_pct": round(other_p / total_parcels * 100, 1) if total_parcels > 0 else 0,
        })

    # 3. Build Grouped Agent Detail Pages
    grouped_index_rows = []
    for slug, g in grouped_map.items():
        clusters = sorted(g["clusters"].values(), key=lambda x: -x["parcel_count"])
        total_parcels = sum(c["parcel_count"] for c in clusters)
        corp_p = g["corporate_parcel_count"]
        inst_p = g["institutional_parcel_count"]
        other_p = max(0, total_parcels - corp_p - inst_p)
        
        html = grouped_detail_tmpl.render(
            page_title=f"{g['name']} — Registered Agent",
            meta_description=f"{g['name']} is a registered agent for {len(clusters)} owner clusters in Atlanta.",
            agent_name=g["name"],
            cluster_count=len(clusters),
            total_parcels=total_parcels,
            clusters=clusters,
            account_count=len(g["accounts"]),
            accounts=sorted(g["accounts"], key=lambda a: (-a["cluster_count"], a["ra_id"])),
            last_updated_str=last_updated_str,
        )
        out_path = output_dir / "agent" / "by-name" / g["slug"] / "index.html"
        write_if_changed(out_path, html)
        written += 1
        grouped_index_rows.append({
            "name": g["name"],
            "slug": g["slug"],
            "account_count": len(g["accounts"]),
            "cluster_count": len(clusters),
            "total_parcels": total_parcels,
            "is_corporate": g["is_corporate"],
            "is_institutional": g["is_institutional"],
            "corporate_parcel_count": corp_p,
            "institutional_parcel_count": inst_p,
            "corp_pct": round(corp_p / total_parcels * 100, 1) if total_parcels > 0 else 0,
            "inst_pct": round(inst_p / total_parcels * 100, 1) if total_parcels > 0 else 0,
            "other_pct": round(other_p / total_parcels * 100, 1) if total_parcels > 0 else 0,
        })

    # 4. Build Index Pages
    # Individual Index (/l/agents/all/)
    index_rows.sort(key=lambda r: (-r["total_parcels"], r["name"]))
    index_html = index_tmpl.render(
        page_title="Registered Agent Accounts",
        meta_description="Individual registered agent accounts appearing across multiple owner clusters in Atlanta.",
        rows=index_rows,
        total=len(index_rows),
        last_updated_str=last_updated_str,
    )
    for dest in [output_dir / "l" / "agents" / "all" / "index.html",
                 output_dir / "agents" / "all" / "index.html"]:
        write_if_changed(dest, index_html)

    # Grouped Index (/l/agents/)
    grouped_index_rows.sort(key=lambda r: (-r["total_parcels"], r["name"]))
    grouped_index_html = grouped_index_tmpl.render(
        page_title="Registered Agents",
        meta_description="Registered agents appearing across multiple owner clusters in Atlanta, grouped by name.",
        rows=grouped_index_rows,
        total=len(grouped_index_rows),
        last_updated_str=last_updated_str,
    )
    for dest in [output_dir / "l" / "agents" / "index.html",
                 output_dir / "agents" / "index.html"]:
        write_if_changed(dest, grouped_index_html)

    return written


def build_address_pages(address_groups, output_dir, last_updated_str=None):
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
            last_updated_str=last_updated_str,
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
        last_updated_str=last_updated_str,
    )
    for dest in [output_dir / "l" / "addresses" / "index.html",
                 output_dir / "addresses" / "index.html"]:
        write_if_changed(dest, index_html)

    print(f"  address pages: {written} pages + index")
    return written


def _build_geo_section(env, area_rows, output_dir, url_base, geo_type_label, area_label,
                       index_title, index_lead, area_display_fn=None, geo_key=None,
                       cluster_connection_count=None, last_updated_str=None,
                       area_buckets=None, area_stats=None, sub_filters=None,
                       write_index=True):
    """Build individual area pages + index page for one geo dimension.
    area_display_fn: optional callable(raw_area) -> display string (e.g. 'District 5')
    write_index: if False, skip writing the index page (caller handles it separately).
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
        
        # Build map URL — supports hometype and zoning as well
        area_raw_enc = quote_plus(str(area))
        if geo_key in ('neighborhood', 'council', 'npu'):
            area_map_url = f"/?geo={geo_key}&area={area_raw_enc}"
        elif geo_key == 'hometype' or area in ["Single-Family", "Multi-Family / Other", "Multi-Family / Condo", "Other"]:
            area_map_url = f"/?hometype={area_raw_enc}"
        elif geo_key == 'zoning' or geo_key == 'city_zoning':
            area_map_url = ""  # no map filter for zoning districts
        elif geo_key == 'county':
            # We don't have a county filter on the map UI yet, but we'll leave it or clear it
            area_map_url = ""
        else:
            area_map_url = ""

        # Aggregate area sparkline from provided area_buckets
        area_spark = None
        if area_buckets and area in area_buckets:
            area_spark = _income_spark(area_buckets[area])

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
            area_spark=area_spark,
            area_stats=area_stats.get(area) if area_stats else None,
            geo_type_label=geo_type_label,
            index_url=index_url,
            index_label=index_title,
            geo_key=geo_key,
            area_raw_enc=area_raw_enc,
            area_map_url=area_map_url,
            rows=top100,
            total=len(top100),
            area_total_parcels=area_total,
            last_updated_str=last_updated_str,
            sub_filters=sub_filters
        )
        out_path = output_dir / slug / "index.html"
        write_if_changed(out_path, html)
        written += 1
        index_rows.append({
            "area": display,
            "url": f"/{url_base}/{slug}/",
            "total_parcels": area_total,
            "top_owner": filtered[0]["primary_name"] if filtered else "",
            "top_owner_spark": filtered[0]["income_spark"] if filtered else None,
            "area_spark": area_spark,
            "map_url": area_map_url,
        })

    index_rows.sort(key=lambda r: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', r["area"])])

    if write_index:
        index_html = idx_tmpl.render(
            page_title=index_title,
            meta_description=index_lead,
            index_title=index_title,
            index_lead=index_lead,
            area_label=area_label,
            rows=index_rows,
            total=len(index_rows),
            last_updated_str=last_updated_str,
        )
        idx_path = output_dir / "index.html"
        write_if_changed(idx_path, index_html)

    return written, index_rows


def build_geo_leaderboard_pages(conn, output_dir, cluster_connection_count=None, last_updated_str=None):
    """Generate all geo leaderboard pages under /l/."""
    env = _make_env()
    base = output_dir / "l"

    if last_updated_str is None:
        last_updated_str = fetch_last_update(conn)

    print("Building geo leaderboards...")

    # Atlanta neighborhoods
    print("  neighborhood...", end=" ", flush=True)
    nbhd_data, nbhd_buckets, nbhd_stats = fetch_geo_data(conn, "city_neighborhood")
    n, _ = _build_geo_section(
        env, nbhd_data, base / "atlanta" / "neighborhood",
        url_base="l/atlanta/neighborhood",
        geo_type_label="Atlanta neighborhood",
        area_label="Neighborhood",
        index_title="Atlanta Neighborhoods",
        index_lead="Top property owners by Atlanta neighborhood.",
        geo_key="neighborhood",
        cluster_connection_count=cluster_connection_count,
        last_updated_str=last_updated_str,
        area_buckets=nbhd_buckets,
        area_stats=nbhd_stats
    )
    print(f"{n} pages")

    # Atlanta council districts
    print("  council...", end=" ", flush=True)
    council_data, council_buckets, council_stats = fetch_geo_data(conn, "city_council")
    n, _ = _build_geo_section(
        env, council_data, base / "atlanta" / "council",
        url_base="l/atlanta/council",
        geo_type_label="Atlanta council district",
        area_label="District",
        index_title="Atlanta City Council Districts",
        index_lead="Top property owners by Atlanta City Council district.",
        area_display_fn=lambda v: f"District {v}",
        geo_key="council",
        cluster_connection_count=cluster_connection_count,
        last_updated_str=last_updated_str,
        area_buckets=council_buckets,
        area_stats=council_stats
    )
    print(f"{n} pages")

    # Atlanta NPUs
    print("  npu...", end=" ", flush=True)
    npu_data, npu_buckets, npu_stats = fetch_geo_data(conn, "city_npu")
    n, _ = _build_geo_section(
        env, npu_data, base / "atlanta" / "npu",
        url_base="l/atlanta/npu",
        geo_type_label="Atlanta NPU",
        area_label="NPU",
        index_title="Atlanta NPUs",
        index_lead="Top property owners by Atlanta Neighborhood Planning Unit (NPU).",
        area_display_fn=lambda v: f"NPU {v}",
        geo_key="npu",
        cluster_connection_count=cluster_connection_count,
        last_updated_str=last_updated_str,
        area_buckets=npu_buckets,
        area_stats=npu_stats
    )
    print(f"{n} pages")

    # Atlanta Zoning — two separate sections: home types + zoning districts
    print("  atlanta zoning...", end=" ", flush=True)
    zoning_data, _, _ = fetch_atlanta_zoning_geo_data(conn)
    atl_ht_data = fetch_atl_hometype_geo_data(conn)

    zoning_dir = base / "atlanta" / "zoning"
    index_url = "/l/atlanta/zoning/"
    index_label = "Atlanta Zoning & Home Types"

    # Build home type detail pages (no index — we write a custom one below)
    n_ht, ht_index_rows = _build_geo_section(
        env, atl_ht_data, zoning_dir,
        url_base="l/atlanta/zoning",
        geo_type_label="Atlanta home type",
        area_label="Home Type",
        index_title=index_label,
        index_lead="Top property owners by Atlanta residential zoning district and home type.",
        geo_key="hometype",
        cluster_connection_count=cluster_connection_count,
        last_updated_str=last_updated_str,
        write_index=False
    )
    # Re-order home type rows by HOME_TYPES_ORDER
    ht_order = {k: i for i, k in enumerate(HOME_TYPES_ORDER)}
    ht_index_rows.sort(key=lambda r: ht_order.get(r["area"], 99))

    # Build zoning district detail pages (no index)
    n_zoning, zoning_index_rows = _build_geo_section(
        env, zoning_data, zoning_dir,
        url_base="l/atlanta/zoning",
        geo_type_label="Atlanta zoning district",
        area_label="Zoning District",
        index_title=index_label,
        index_lead="Top property owners by Atlanta residential zoning district and home type.",
        geo_key="city_zoning",
        cluster_connection_count=cluster_connection_count,
        last_updated_str=last_updated_str,
        write_index=False
    )

    # Write the custom two-section index page
    atl_zoning_idx_tmpl = env.from_string(ATL_ZONING_INDEX_TMPL)
    idx_html = atl_zoning_idx_tmpl.render(
        page_title="Atlanta Zoning & Home Types",
        meta_description="Top property owners by Atlanta residential zoning district and home type.",
        ht_rows=ht_index_rows,
        zoning_rows=zoning_index_rows,
        last_updated_str=last_updated_str,
    )
    write_if_changed(zoning_dir / "index.html", idx_html)
    print(f"{n_ht + n_zoning} pages")

    # County leaderboards with Home Type sub-filters
    print("  counties...", end=" ", flush=True)
    county_type_data = fetch_county_geo_data(conn)
    # Group by county: {county: {type: [rows]}}
    counties = defaultdict(dict)
    for (county, htype), rows in county_type_data.items():
        counties[county][htype] = rows

    HOME_TYPES = ["Single-Family", "Multi-Family / Other", "Multi-Family / Condo", "Other"]
    
    written = 0
    for county in ["fulton", "dekalb"]:
        types = counties.get(county, {})
        
        # Re-fetch "All" rows for the county specifically
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT oe.cluster_id, mc.owner_names[1] AS primary_name,
                       mc.parcel_count AS total_parcel_count, COUNT(*) AS local_parcel_count,
                       mc.corporate_parcel_count > 0 AS is_corporate,
                       mc.institutional_parcel_count > 0 AS is_institutional,
                       mc.primary_foreign_state, mc.owner_names[2:4] AS alt_names_arr
                FROM owner_entities oe
                CROSS JOIN LATERAL unnest(oe.parcel_ids) AS pid
                JOIN parcels_unified p ON p.parcel_id = pid AND p.county = oe.county
                JOIN mv_cluster_stats mc ON mc.cluster_id = oe.cluster_id
                WHERE p.county = %s
                GROUP BY 1, 2, 3, 5, 6, 7, 8 ORDER BY local_parcel_count DESC
            """, (county,))
            county_all_rows = []
            for row in cur.fetchall():
                alts = [n for n in (row["alt_names_arr"] or []) if n]
                county_all_rows.append({
                    **dict(row), "alt_names": ", ".join(alts) if alts else "",
                    "connection_count": 0, "income_spark": None
                })

        sub_filters = [{"label": "All", "url": f"/l/county/{county}/", "active": True}]
        for ht in HOME_TYPES:
            sub_filters.append({"label": ht, "url": f"/l/county/{county}/{slugify(ht)}/", "active": False})

        # Render All page
        n, _ = _build_geo_section(
            env, {county: county_all_rows}, base / "county",
            url_base="l/county", geo_type_label="county", area_label="County",
            index_title="Counties", index_lead="Top landlords by county.",
            area_display_fn=lambda v: v.title(), geo_key="county",
            cluster_connection_count=cluster_connection_count,
            last_updated_str=last_updated_str, sub_filters=sub_filters
        )
        written += n

        # Render Home Type sub-pages
        for ht in HOME_TYPES:
            ht_rows = types.get(ht, [])
            ht_slug = slugify(ht)
            # Update sub-filters for this specific page
            ht_sub_filters = []
            for sf in sub_filters:
                ht_sub_filters.append({**sf, "active": (sf["label"] == ht)})
            
            n, _ = _build_geo_section(
                env, {ht: ht_rows}, base / "county" / county,
                url_base=f"l/county/{county}", geo_type_label=f"{ht} in {county.title()}",
                area_label="Type", index_title=f"{county.title()} Home Types",
                index_lead=f"Top {ht} owners in {county.title()}.",
                geo_key="hometype", # map link will use ?hometype=...
                cluster_connection_count=cluster_connection_count,
                last_updated_str=last_updated_str, sub_filters=ht_sub_filters,
                write_index=False  # don't overwrite the county "All" page
            )
            written += n
    print(f"{written} pages")


def fetch_portfolio_demographics_batch(conn, cluster_ids):
    """Returns {cluster_id: {atlanta_parcel_count, avg_income, avg_renter, avg_white, avg_black, income_buckets, market_share, ...}}"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT cluster_id, atlanta_parcel_count, avg_neighborhood_income, avg_neighborhood_renter_pct,
                   avg_neighborhood_white_pct, avg_neighborhood_black_pct,
                   income_bucket_counts, market_share_json,
                   avg_neighborhood_hispanic_pct, avg_neighborhood_asian_pct,
                   avg_neighborhood_poverty_pct, avg_neighborhood_home_value,
                   avg_neighborhood_vacant_pct, home_value_bucket_counts
            FROM portfolio_demographics
            WHERE cluster_id = ANY(%s)
        """, (cluster_ids,))
        return {row["cluster_id"]: dict(row) for row in cur.fetchall()}

def worker(args):
    """Worker function run in a subprocess. Processes a slice of cluster_ids."""
    cluster_ids, output_dir, db_url, worker_id, linkable_agent_ids, cluster_related, last_updated_str = args
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
            demo_map       = fetch_portfolio_demographics_batch(conn, batch)

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
                demographics   = demo_map.get(cid)
                html = render_owner(
                    cid, stats, parcels, county_breakdown, sos_data, neighborhoods,
                    linkable_agent_ids, cluster_related,
                    entity_sos_ids=entity_sos_ids,
                    officers=officers,
                    demographics=demographics,
                    last_updated_str=last_updated_str
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

    last_updated_str = fetch_last_update(conn)

    # Split cluster_ids evenly across workers
    chunks = [cluster_ids[i::num_workers] for i in range(num_workers)]
    work_args = [(chunk, str(output_dir), DB_URL, i, linkable_agent_ids, cluster_related or {}, last_updated_str) 
                 for i, chunk in enumerate(chunks)]

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
            last_updated_str = fetch_last_update(conn)
            build_leaderboard(conn, output_dir, cluster_connection_count, last_updated_str)
            build_geo_leaderboard_pages(conn, output_dir,
                                         cluster_connection_count=cluster_connection_count,
                                         last_updated_str=last_updated_str)
            build_numbers_page(conn, output_dir, last_updated_str)
            # Agent pages are fast; always build unless owner-only
            print("Building agent pages...", end=" ", flush=True)
            n_agents = build_agent_pages(linkable_agents, agent_clusters, output_dir, last_updated_str)
            print(f"done ({n_agents} pages)")
            # Shared address pages
            print("Building shared address pages...", end=" ", flush=True)
            build_address_pages(address_groups, output_dir, last_updated_str)
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
