"""Load GA SOS bulk data into sos schema in PostGIS.

Files (all TSV, CRLF line endings):
  BizEntity.txt                 ~4.3M rows  — core entity info
  BizEntityAddress.txt          ~4.7M rows  — principal addresses
  BizEntityOfficers.txt         ~49M rows   — officers/directors
  BizEntityRegisteredAgents.txt ~10M rows   — registered agents (ISO-8859)

Skipped for now: BizEntityFilingHistory.txt, BizEntityStock.txt
"""

import io
import json
import os
import psycopg2
from pathlib import Path

DB = dict(host="localhost", port=5434, dbname="who_owns_atl", user="woa", password="woa")

def _load_sources():
    root = Path(__file__).resolve().parent.parent
    return json.load(open(root / "web/frontend/data/datasources.json"))

DATA_DIR = _load_sources()["ga_sos"]["file_path"]

CHUNK = 500_000  # rows per COPY batch for large files


# ---------------------------------------------------------------------------
# Schema / table DDL
# ---------------------------------------------------------------------------

DDL = """
CREATE SCHEMA IF NOT EXISTS sos;

DROP TABLE IF EXISTS sos.entities CASCADE;
CREATE TABLE sos.entities (
    control_number      TEXT,
    business_id         TEXT,
    business_name       TEXT,
    business_type_desc  TEXT,
    commencement_date   TEXT,
    effective_date      TEXT,
    is_perpetual        TEXT,
    end_date            TEXT,
    foreign_state       TEXT,
    foreign_country     TEXT,
    foreign_date_of_org TEXT,
    entity_status_date  TEXT,
    entity_status       TEXT,
    registered_agent_id TEXT,
    good_standing       TEXT,
    phone_number        TEXT,
    email_address       TEXT,
    naics_code          TEXT,
    naics_sub_code      TEXT
);

DROP TABLE IF EXISTS sos.addresses CASCADE;
CREATE TABLE sos.addresses (
    business_id      TEXT,
    control_number   TEXT,
    street_address1  TEXT,
    street_address2  TEXT,
    city             TEXT,
    state            TEXT,
    zip              TEXT,
    country          TEXT
);

DROP TABLE IF EXISTS sos.officers CASCADE;
CREATE TABLE sos.officers (
    control_number  TEXT,
    description     TEXT,
    first_name      TEXT,
    middle_name     TEXT,
    last_name       TEXT,
    company_name    TEXT,
    line1           TEXT,
    line2           TEXT,
    city            TEXT,
    state           TEXT,
    zip             TEXT,
    filing_no       TEXT,
    business_id     TEXT
);

DROP TABLE IF EXISTS sos.registered_agents CASCADE;
CREATE TABLE sos.registered_agents (
    registered_agent_id TEXT,
    name                TEXT,
    commercial_ra       TEXT,
    line1               TEXT,
    line2               TEXT,
    line3               TEXT,
    line4               TEXT,
    city                TEXT,
    state               TEXT,
    zip                 TEXT,
    phone_number        TEXT,
    email               TEXT,
    county_name         TEXT,
    country             TEXT
);
"""

INDEXES = """
CREATE INDEX ON sos.entities (control_number);
CREATE INDEX ON sos.entities (business_id);
CREATE INDEX ON sos.entities (registered_agent_id);
CREATE INDEX ON sos.entities (entity_status);
CREATE INDEX ON sos.entities USING gin (to_tsvector('simple', coalesce(business_name, '')));

CREATE INDEX ON sos.addresses (business_id);
CREATE INDEX ON sos.addresses (control_number);

CREATE INDEX ON sos.officers (control_number);
CREATE INDEX ON sos.officers (business_id);

CREATE INDEX ON sos.registered_agents (registered_agent_id);

-- pg_trgm index for fuzzy name matching against parcel owner names
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ON sos.entities USING gin (business_name gin_trgm_ops);
"""


# ---------------------------------------------------------------------------
# Column maps: TSV header → DB column name (in order)
# ---------------------------------------------------------------------------

ENTITY_COLS = [
    "control_number", "business_id", "business_name", "business_type_desc",
    "commencement_date", "effective_date", "is_perpetual", "end_date",
    "foreign_state", "foreign_country", "foreign_date_of_org",
    "entity_status_date", "entity_status", "registered_agent_id",
    "good_standing", "phone_number", "email_address", "naics_code", "naics_sub_code",
]

ADDRESS_COLS = [
    "business_id", "control_number", "street_address1", "street_address2",
    "city", "state", "zip", "country",
]

OFFICER_COLS = [
    "control_number", "description", "first_name", "middle_name", "last_name",
    "company_name", "line1", "line2", "city", "state", "zip",
    "filing_no", "business_id",
]

RA_COLS = [
    "registered_agent_id", "name", "commercial_ra",
    "line1", "line2", "line3", "line4",
    "city", "state", "zip", "phone_number", "email", "county_name", "country",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def copy_tsv_file(conn, table, columns, filepath, encoding="ascii", chunk_size=CHUNK):
    """Stream a TSV file into a table via COPY, skipping the header row."""
    cur = conn.cursor()
    col_list = ", ".join(columns)
    copy_sql = f"COPY sos.{table} ({col_list}) FROM STDIN WITH (FORMAT TEXT, DELIMITER E'\\t', NULL '')"

    total = 0
    buf = io.StringIO()
    with open(filepath, encoding=encoding, errors="replace", newline="") as fh:
        next(fh)  # skip header
        for line in fh:
            line = line.rstrip("\r\n")
            # Escape backslashes (COPY text format treats \ as escape)
            line = line.replace("\\", "\\\\")
            # Normalize literal "NULL" strings to empty (so they load as SQL NULL)
            line = "\t".join(
                "" if f == "NULL" else f for f in line.split("\t")
            )
            # Normalize column count (pad short rows, truncate long rows)
            fields = line.split("\t")
            n = len(columns)
            if len(fields) < n:
                fields += [""] * (n - len(fields))
            elif len(fields) > n:
                fields = fields[:n]
            buf.write("\t".join(fields) + "\n")
            total += 1
            if total % chunk_size == 0:
                buf.seek(0)
                cur.copy_expert(copy_sql, buf)
                buf = io.StringIO()
                print(f"    {total:,} rows...", flush=True)

        if buf.tell():
            buf.seek(0)
            cur.copy_expert(copy_sql, buf)

    conn.commit()
    cur.close()
    print(f"  Done: {total:,} rows → sos.{table}")
    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()

    print("Creating sos schema and tables...")
    cur.execute(DDL)
    conn.commit()
    cur.close()

    files = [
        ("entities",          ENTITY_COLS,  "BizEntity.txt",                   "ascii"),
        ("addresses",         ADDRESS_COLS, "BizEntityAddress.txt",             "ascii"),
        ("registered_agents", RA_COLS,      "BizEntityRegisteredAgents.txt",    "latin-1"),
        ("officers",          OFFICER_COLS, "BizEntityOfficers.txt",            "ascii"),
    ]

    for (table, cols, filename, enc) in files:
        path = os.path.join(DATA_DIR, filename)
        print(f"\nLoading {filename} → sos.{table}  ({enc})")
        copy_tsv_file(conn, table, cols, path, encoding=enc)

    print("\nBuilding indexes...")
    cur = conn.cursor()
    for stmt in INDEXES.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            print(f"  {stmt[:60]}...")
            cur.execute(stmt)
            conn.commit()
    cur.close()

    conn.close()
    print("\nAll done.")


if __name__ == "__main__":
    main()
