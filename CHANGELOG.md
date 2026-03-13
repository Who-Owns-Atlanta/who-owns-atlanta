# Changelog — Who Owns Atlanta

Releases follow [CalVer](https://calver.org/) format: `YYYYMMx.N`
- `YYYYMM` = data vintage year + month
- `x` = data release letter within that month (A = first pipeline run, B = second, etc.)
- `.N` = code/interface increment within a data release

---

## v202603B.1 — 2026-03-12

Second pipeline run. March 2026 data, second pass.

- 615,955 parcels (unchanged)
- 469,582 ownership clusters (+2,001 from improved deduplication)
- Zoning data integrated (land use codes, home type filtering)
- Improved institutional owner flagging (+2,897 reclassified from corporate)
- +482 additional SOS entity matches
- Deployment: new `bq_*` pipeline tables excluded from prod dump (keeps dump ~280MB)
- Fixed nginx maintenance mode pattern (server-level `return 503` avoids location conflict)

---

## v202603A.1 — 2026-03-08

First public release. March 2026 data.

- 615,955 parcels (Fulton + DeKalb counties)
- 467,581 ownership clusters
- Initial production deployment
- Cluster mode deep links (`?cluster=ID`)
- Owner profile pages with parcel lists
- Vector tile map with corporate/institutional/individual color coding
