# Setup Log

## 2026-02-12 — Project Initialization

### Steps completed

1. **Git initialized** — `git init` in project root
2. **`.gitignore` created** — excludes:
   - `data/json/geojson/latest/*.json` and `*.geojson` (large data files, ~1.7GB total)
   - `.venv/`, `__pycache__/`, `.env`, IDE files
3. **`uv init --name who-owns-atl --python 3.12`** — created `pyproject.toml`
   - Python 3.12.3 (system) selected
   - `uv sync` verified — venv at `.venv/` working
4. **`planning/` directory created** — this file and project inventory

### Current file tree (non-data)
```
who_owns_atl/
├── .git/
├── .gitignore
├── .python-version          (3.12, in .gitignore)
├── .venv/
├── pyproject.toml
├── uv.lock
├── CLAUDE.md -> AGENTS.md   (symlink, do not overwrite)
├── docs/
│   ├── project_start.md
│   ├── horizontal-holdings.pdf
│   ├── workflow_setup_cg.md
│   ├── workflow_setup_gk.md
│   └── workflow_setup_gk.pdf
├── data/
│   └── json/geojson/latest/  (gitignored, ~1.7GB)
├── planning/                 (this directory)
└── tmp_nbh_accela -> ../nbh_accela/  (symlink to sibling repo)
```
