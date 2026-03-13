# Dump Manifest

Database dumps are excluded from git (`.gitignore`). This file tracks what was produced per release.

| Version | Date | Parcels | Clusters | Archived DB | Sources File | Notes |
|---------|------|---------|----------|-------------|--------------|-------|
| v202603A.1 | 2026-03-08 | 615,955 | 467,581 | woa_v202603a1 | v202603A.1.sources.json | First production pipeline run. Fulton + DeKalb. |
| v202603B.1 | 2026-03-12 | 615,955 | 469,582 | woa_v202603b1 | v202603B.1.sources.json | Zoning data added; improved institutional flagging (+2,897), better clustering, +482 SOS matches. Dump excludes bq_*/addr_norm_lookup pipeline tables. |
