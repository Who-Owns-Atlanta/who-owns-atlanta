# 18 — "By the Numbers" Citywide Findings Page

## Status: implemented (2026-03-06)

## Context

Two new materialized views (`mv_ownership_demographics`, `mv_ownership_by_income_quartile`)
were created in `scripts/sql/04_create_materialized_views.sql` to answer:
"How does ownership type correspond to neighborhood wealth?"

These views aggregate parcel-level neighborhood demographics across ~168k Atlanta city parcels
(those with `city_neighborhood` set), grouped by ownership type (corporate / institutional / individual).

## What the Data Shows (summary)

| | Individual | Corporate | Institutional |
|---|---|---|---|
| Parcels | 122k | 30k | 15k |
| Avg nbhd income | $101,655 | **$75,754** | $91,173 |
| Median nbhd income | $104,600 | **$58,579** | $80,321 |
| Avg Black% | 44% | **60%** | 51% |
| Avg poverty% | 2.4% | **4.9%** | 3.6% |
| Avg renter% | 48% | **58%** | 54% |

Corporate parcels skew heavily to Q1/Q2 (lowest income): 67% of corporate parcels are in the
bottom two income quartiles vs 45% of individual parcels.

## Page Design

**URL:** `/numbers/`
**Title:** "By the Numbers"
**Tone:** Analytical — draws explicit comparisons with numbers
**Style:** `.content-page` body class, same as About/Methodology

### Sections

1. **Lede** — Key finding in 2–3 sentences with inline numbers
2. **Summary comparison table** — 3 columns (corporate / institutional / individual):
   - Parcel count (% of total)
   - Avg + median neighborhood income
   - Avg Black%, White%, Hispanic%, Asian%, Other% (all races)
   - Avg poverty%, vacancy%, renter%
   - Avg home value
3. **Racial composition bars** — Three `.race-bar` rows (one per ownership type),
   using existing `content.css` colors
4. **Income quartile distribution** — For each ownership type: horizontal bar showing
   % of parcels in Q1–Q4 (Q1 = <$52k, Q4 = >$132k median neighborhood income)
5. **Footnotes / methodology** — Coverage, data vintage, "other" race definition,
   Q4 corporate poverty anomaly (likely LIHTC/subsidized housing), link to /methodology/

## Implementation

### Files to change

1. **`scripts/build_static_pages.py`**
   - `fetch_ownership_demographics(conn)` — queries both MVs
   - `render_numbers_page(data)` — Jinja2 inline template
   - `build_numbers_page(conn, output_dir)` — outputs `/numbers/index.html`
   - Call in `main()` alongside leaderboard builds

2. **`web/frontend/about/index.html`** — add nav link to `/numbers/`

3. **`web/frontend/methodology/index.html`** — add nav link to `/numbers/`

4. **`LEADERBOARD_TMPL`** in `build_static_pages.py` — add nav link to `/numbers/`

### Reused CSS classes (no new CSS needed)
- `.content-page`, `.content-main`, `.content-prose` — page layout
- `.demographics-grid`, `.demo-card` — card grid
- `.race-bar`, `.race-segment`, `.race-legend`, `.race-legend-item`, `.race-dot` — race bars
- `.bucket-row`, `.bucket-bar-bg`, `.bucket-bar`, `.bucket-label`, `.bucket-count` — bar charts

### Data queries
```sql
-- 3 rows
SELECT * FROM mv_ownership_demographics ORDER BY parcel_count DESC;

-- 12 rows (3 types × 4 quartiles)
SELECT * FROM mv_ownership_by_income_quartile ORDER BY income_quartile, owner_type;
```

## Verification

```bash
uv run scripts/build_static_pages.py --leaderboard-only  # verify MVs accessible
uv run scripts/build_static_pages.py                     # full build

shot-scraper http://who-owns-atlanta.local/numbers/ -o /tmp/numbers.png --width 1200
shot-scraper http://who-owns-atlanta.local/numbers/ -o /tmp/numbers_mobile.png --width 390
```

## Known Issues / Caveats

- **Q4 corporate poverty anomaly:** 11% avg poverty for corporate in Q4 vs 0.5% for individual.
  Likely large LIHTC/subsidized apartment portfolios (e.g., public housing adjacent or LIHTC
  in high-value neighborhoods). Footnote in the page.
- **"Other" race:** computed as `GREATEST(0, 100 - black - white - hispanic - asian)`.
  Source data doesn't have explicit AIAN/multiracial fields; avg remainder is ~3%.
- **Coverage:** Only Atlanta city-boundary parcels with `city_neighborhood` set (~168k of 616k total).
  Does NOT cover unincorporated Fulton/DeKalb outside city limits.
- **Neighborhood demographics vintage:** 2024 (2020 Census + ACS 5-year estimates).
