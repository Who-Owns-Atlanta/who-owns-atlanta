# 11b — Demographic Data Pipeline

## Overview

The demographic pipeline enriches owner portfolio profiles with neighborhood-level data, enabling the "Atlanta Portfolio Analysis" section on owner profile pages. It aggregates Census/ACS-derived neighborhood statistics across all parcels in a portfolio cluster.

---

## Data Source

**File:** `Official_Neighborhoods_with_Current_Demographic_Data_(2024).geojson`
**Location:** `/home/jesse/projects/data/gis_json/geojson/latest/`
**Coverage:** 248 Atlanta neighborhoods
**Vintage:** 2024 (based on 2020 Census + ACS estimates)

---

## Field Mapping (`06c_load_neighborhood_demographics.py`)

| GeoJSON field | DB column | Notes |
|---|---|---|
| `NAME` | `neighborhood_name` | Join key to parcel data |
| `populati_1` | `total_population` | |
| `gender_MED` | `median_age` | |
| `householdt` | `total_households` | Denominator for poverty % |
| `households` | `below_poverty_count` | HH below poverty threshold |
| `OwnerRente` | `owner_occupied_count` | |
| `OwnerRen_1` | `owner_occupied_pct` | |
| `OwnerRen_2` | `renter_occupied_count` | |
| `OwnerRen_3` | `renter_occupied_pct` | |
| `housinguni` | `total_housing_units` | |
| `vacant_VAC` | `vacant_units_count` | |
| `vacant_V_1` | `vacant_units_pct` | |
| `raceandh_1` | `white_pct` | |
| `raceandh_3` | `black_pct` | |
| `raceandh_5` | `asian_pct` | |
| `hispanic_1` | `hispanic_pct` | |
| `householdi` | `median_household_income` | |
| `homevalue_` | `median_home_value` | |
| `educatio_5` | `bachelors_degree_pct` | |
| `educatio_6` | `graduate_degree_pct` | |
| `househol_1` | `avg_household_size` | |

**Schema:** `gis.neighborhood_demographics`
**Load strategy:** `if_exists="replace"` (full truncate + reload on each run)
**Indexes:** GIST on `geometry`, btree on `neighborhood_name`

---

## Portfolio Aggregation (`12_portfolio_demographics.py`)

### Target table: `portfolio_demographics`

| Column | Type | Description |
|---|---|---|
| `cluster_id` | INTEGER PK | Owner cluster |
| `atlanta_parcel_count` | INTEGER | Parcels with matched neighborhood |
| `avg_neighborhood_income` | NUMERIC | Avg median HH income across parcels |
| `avg_neighborhood_renter_pct` | NUMERIC | Avg renter % |
| `avg_neighborhood_white_pct` | NUMERIC | Avg white % |
| `avg_neighborhood_black_pct` | NUMERIC | Avg Black % |
| `avg_neighborhood_hispanic_pct` | NUMERIC | Avg Hispanic % |
| `avg_neighborhood_asian_pct` | NUMERIC | Avg Asian % |
| `avg_neighborhood_poverty_pct` | NUMERIC | Avg % HH below poverty |
| `avg_neighborhood_home_value` | NUMERIC | Avg median home value |
| `avg_neighborhood_vacant_pct` | NUMERIC | Avg vacant units % |
| `income_bucket_counts` | JSONB | `{Low, Low-Mid, Mid, Mid-High, High: count}` |
| `home_value_bucket_counts` | JSONB | `{<$150k, $150-300k, $300-500k, $500k+: count}` |
| `market_share_json` | JSONB | `{neighborhood: {parcels, rental_share}}` |
| `last_updated` | TIMESTAMP | |

### Eligibility threshold

Only clusters with `parcel_count >= 10` (from `mv_cluster_stats`) get demographics calculated. Smaller portfolios don't have enough geographic spread to be meaningful.

### CTE structure

```
cluster_parcels     → join owner_entities → parcels_unified → get city_neighborhood per parcel
neighborhood_stats  → AVG all metrics per cluster (income, renter, race, poverty, home value, vacancy)
income_buckets      → bucket each parcel's neighborhood income, jsonb_object_agg per cluster
home_value_buckets  → bucket each parcel's neighborhood home value, jsonb_object_agg per cluster
market_shares       → per-neighborhood rental share (this cluster's parcels / total rentals in nbhd)
```

### Poverty rate calculation

`below_poverty_count` is an absolute count of households. The rate is computed on the fly:
```sql
AVG(d.below_poverty_count::float / NULLIF(d.total_households, 0) * 100)
```

### Income bucket thresholds

| Bucket | Range |
|---|---|
| Low | < $40,000 |
| Low-Mid | $40k – $57k |
| Mid | $57k – $84k |
| Mid-High | $84k – $136k |
| High | ≥ $136k |

### Home value bucket thresholds

| Bucket | Range |
|---|---|
| `<$150k` | < $150,000 |
| `$150-300k` | $150k – $300k |
| `$300-500k` | $300k – $500k |
| `$500k+` | ≥ $500,000 |

---

## Rendering (`build_static_pages.py`)

### Fetch

`fetch_portfolio_demographics_batch()` selects all 14 columns for a batch of cluster IDs.

### Data prep in `render_owner()`

1. Float-cast all 9 numeric avg columns (null → 0)
2. Sort income buckets: `['Low', 'Low-Mid', 'Mid', 'Mid-High', 'High']`
3. Sort home value buckets: `['<$150k', '$150-300k', '$300-500k', '$500k+']`
4. `other_pct` computed as `max(0, 100 - black - white - hispanic - asian)` for the race bar remainder

### Template cards (4-card 2×2 grid)

| Card | Heading | Key visuals |
|---|---|---|
| 1 | Neighborhood Income | Avg income stat + 5-bucket horizontal bar chart |
| 2 | Tenure & Concentration | Avg renter % + top-3 neighborhood market shares |
| 3 | Racial Composition | Avg % by group + stacked color bar + legend |
| 4 | Home Values & Vulnerability | Avg home value + 4-bucket bar chart + poverty/vacancy stats |

### Race bar colors

| Group | Color |
|---|---|
| Black | `#6366f1` (indigo) |
| White | `#94a3b8` (slate) |
| Hispanic | `#f59e0b` (amber) |
| Asian | `#10b981` (emerald) |
| Other | `#e2e8f0` (light gray) |

---

## CSS classes added (`content.css`)

- `.race-bar` — flex container for stacked race segments
- `.race-segment` — individual color segment (width = pct%)
- `.race-legend` / `.race-legend-item` / `.race-dot` — legend below the bar
- `.vuln-stats` / `.vuln-stat` — poverty + vacancy stat row at bottom of card 4

---

## Run order

```bash
uv run scripts/06c_load_neighborhood_demographics.py   # reload gis.neighborhood_demographics
uv run scripts/12_portfolio_demographics.py             # regenerate portfolio_demographics
uv run scripts/build_static_pages.py                   # rebuild all owner pages
```

Or for a single cluster test:
```bash
uv run scripts/build_static_pages.py --owner-only --cluster-ids 8
```

---

## Known gaps / future work

- `other_pct` is inferred as remainder; the source data doesn't have an explicit "other" race field
- Two-or-more-races population not represented
- Poverty threshold used is household-level (not individual poverty rate)
- `below_poverty_count` appears as `0` for many outer-Atlanta neighborhoods — likely sparse ACS coverage, not true zero-poverty areas
- Demographic data vintage is 2024 (based on ACS 5-year estimates); parcel data is updated more frequently
