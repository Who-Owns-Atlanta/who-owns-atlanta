# DEVELOPMENT ENVIRONMENT GUIDELINES

- **IMPORTANT** This file should be AGENTS.md . IF `CLAUDE.md` or `GEMINI.md` exist, they should be symlinks to this file. DO NOT OVERWRITE THE SYMLINKS.

- **IMPORTANT** After significant changes, make sure to ASK about commiting and updating this file as necessary.

- `python` scripting and web environment managed with `uv`, packaged under `docker` for production


# TOOLS
Don't ask permission before running read-only or non-destructive commands.
If a command only reads/lists/searches—it doesn't write, delete, move, or
modify—run it immediately without narrating your intent first.

Most commong linux tools exist; use any tools liberally. These tools are extrememly releve to this project:
- `uv` - this is a managed `Python` project.
- `curl` - check the api or website yourself!
- `git` - this is under source control with `git`.
- `psql` - check your postgres/gis queries!
    - you MUST add PGPASSWORD=  for cli commands to succeed
- `rg`, grep  (ripgrep)
- `shot-scraper` - cli tool to take screeshots of web pages so you can "view" your changes.  [local, --help docs](docs/shot-scraper_help.txt) , [fuller, remote docs](https://shot-scraper.datasette.io/en/stable/screenshots.html). DO NOT PREVIEW EMAIL HTML


