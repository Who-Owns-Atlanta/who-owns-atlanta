# Who Owns Atlanta?

A public tool for exploring property ownership in Atlanta and across Fulton and DeKalb counties. Search any address to find who owns it, whether the owner is a corporation or institution, and follow the ownership network across the city. Leaderboards and map filters also facilitate further exploration.

**Live site:** [who-owns-atlanta.org](https://who-owns-atlanta.org)

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


## Process

**Claude** and **Gemini** were used heavily - almost exclusively - for code, documentation, and (usually edited) baseline versions of various copy/prose on the website. The code and process is deterministic - there are no LLM calls in the pipeline. There are thoughts of comitting edited verions of the chat prompts/sessions used.


## Tech stack

- **Pipeline:** Python, PostGIS, `uv`
- **Tiles:** tippecanoe, MapLibre GL JS
- **API:** FastAPI
- **Frontend:** vanilla JS, Pico CSS
- **Infrastructure:** nginx, Docker, PostgreSQL/PostGIS

## Dataset size (March 2026)
- 615,955 tax parcels — 370,189 Fulton County + 245,766 DeKalb County
- 523,555 unique owner entities collapsed into 467,581 ownership clusters
- 37,563 clusters owning 2+ parcels; largest single cluster: 2,930 parcels
- Matched 37,070 owner entities to Georgia Secretary of State corporate records (13,171 exact name matches + 23,899 fuzzy trigram matches against 49M SOS officer rows)

## Pipeline / build machine
- Data pipeline runs were on an AMD Ryzen 7 2700X (16 threads), 62GB RAM
- SOS fuzzy match parallelized across all 16 cores via Python multiprocessing
  - `30-60 minutes`
- 135,594 static owner profile pages pre-generated (1.3GB), served directly by nginx — zero DB hits for owner pages
  - `real	4m23.327s
    user	33m51.073s
    sys	0m7.622s
    `
- Vector tiles: 198MB, built with tippecanoe, hosted on Cloudflare R2
  - `
    real	2m1.777s
    user	3m48.626s
    sys	0m10.523s
    `
- Various other timings possibly to come...



## Running it yourself

The [runbook](./planning/06_production_runbook.md) walks through (hopefully) the last major rebuild. Claude and Gemini were used extensively to build and document this project - fed the referenced data and careful prompts, they likely can build it from scratch.

The general process is:

1. Acquire county tax parcel GIS data
2. Run the ingestion pipeline (`uv run` — see `scripts/`)
3. Build ownership clusters
4. Generate vector tiles (`scripts/build_tiles.sh`)
5. Serve with the included nginx config and FastAPI app

### Replicating in other areas
- Any county with public tax parcel + GIS data can be ingested — pipeline is county-agnostic from step 2 onward, though a lot of fiddling will likely be required to map property coding to corporate/instituinal/condos and the like.
- SOS match requires a state corporate registry dump (Georgia's is available for purchase from the SOS website).


## License

AGPL-3.0 — see [LICENSE](LICENSE). If you run a modified version as a public service, you must make your source available.

## Author

[jessedp](https://github.com/jessedp)
