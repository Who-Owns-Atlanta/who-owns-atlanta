# Who Owns Atlanta?

A public tool for exploring property ownership across Fulton and DeKalb counties. Search any Atlanta address to find who owns it, whether the owner is a corporation or institution, and follow the ownership network across the city.

**Live site:** [who-owns-atlanta.org](https://who-owns-atlanta.org) *(coming soon)*

## What it does

- Address search across ~600k parcels in Fulton and DeKalb counties
- Corporate and institutional owner flagging
- Ownership cluster detection — links related LLCs and shell companies through shared addresses, registered agents, and other identifiers
- Interactive map with parcel-level ownership visualization
- Owner profiles with portfolio analysis, neighborhood breakdown, and Secretary of State filings

## Data sources

All underlying data is drawn from public records. No raw data is redistributed by this project.

| Source | What it provides |
|---|---|
| Fulton County Tax Assessor | Parcel ownership records |
| DeKalb County Tax Assessor | Parcel ownership records |
| Georgia Secretary of State | Business entity filings, registered agents, officers |
| US Census Bureau | Neighborhood demographics (ACS) |
| Atlanta Regional Commission | Neighborhood boundaries |

In the State of Georgia, County tax records and Secretary of State business filings are public records under Georgia’s Open Records Act, which provides that “all public records shall be open for personal inspection and copying, except those which by order of a court of this state or by law are specifically exempted.”  

O.C.G.A. § 50‑18‑71(a) (Right of access; timing; fees) – Georgia Open Records Act
https://law.justia.com/codes/georgia/title-50/chapter-18/article-4/section-50-18-71/


## Tech stack

- **Claude** and **Gemini**: heavy LLM usage with semi-informed guidance
- **Pipeline:** Python, PostGIS, `uv`
- **Tiles:** tippecanoe, MapLibre GL JS
- **API:** FastAPI
- **Frontend:** vanilla JS, Pico CSS
- **Infrastructure:** nginx, Docker, PostgreSQL/PostGIS

## Running it yourself

The [runbook](./planning/06_production_runbook.md) walks through (hopefully) the last major rebuild. Claude and Gemini were used extensively to build and document this project - fed the correct data, one likely can again.

The general process is:

1. Acquire county tax parcel GIS data
2. Run the ingestion pipeline (`uv run` — see `scripts/`)
3. Build ownership clusters
4. Generate vector tiles (`scripts/build_tiles.sh`)
5. Serve with the included nginx config and FastAPI app

## License

AGPL-3.0 — see [LICENSE](LICENSE). If you run a modified version as a public service, you must make your source available.

## Author

[jessedp](https://github.com/jessedp)
