"""Flag corporate and institutional owners in both parcel tables.

Two flags:
  is_corporate    — SOS-resolvable business entities (LLC, LP, corp, etc.)
  is_institutional — broader non-individual owners (government, education, trusts, HOAs)
                     tracked in ownership network but not sent to SOS
"""

from sqlalchemy import create_engine, text

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

# SOS-resolvable business entities
# Handles spaced abbreviations: "L L C", "L P", "L.L.C." etc.
CORPORATE_PATTERN = (
    r'\m('
    r'l\s*l\s*c|l\s*l\s*l\s*p|l\s*l\s*p|l\s*p'  # LLC, LLLP, LLP, LP (with optional spaces)
    r'|inc|corp|corporation|ltd|limited'
    r'|assoc|assn|association'
    r'|foundation|company|co\.'
    r'|system|plan|p\s*c'
    r'|venture|ventures|invest|investments|investors'
    r'|partners|partnership'
    r'|holdings|holding|enterprises|enterprise'
    r'|properties|property|realty|real\s+estate'
    r'|management|mgmt|development|group'
    r')\M'
)

# Non-individual owners that are definitely institutional
STRONG_INSTITUTIONAL_PATTERN = (
    r'('
    r'city\s+of|county|state\s+of|united\s+states'
    r'|marta|board\s+of\s+(education|regents)'
    r'|department\s+of|dept\s+of'
    r'|\m(authority|development\s+authority|housing\s+authority)\M'
    r'|\mregents\M.*system'
    r'|college|university|school\s+district|school\s+system'
    r'|railway|railroad|seaboard|norfolk\s+southern|georgia\s+power|bellsouth'
    r')'
)

# Non-individual owners that might clash with address-named LLCs or individuals
# These are checked AFTER strong institutional but BEFORE corporate
MEDIUM_INSTITUTIONAL_PATTERN = (
    r'('
    r'homeowner|homeowners|h\s*o\s*a'
    r'|community\s+associat|owners\s+associat|associat'
    r'|condo|condominium'
    r'|townhouse|towne\s+house'
    r'|wildwood\s+park|oxford\s+village'
    r')'
)

# Non-individual owners that should only be flagged if they don't match corporate patterns
WEAK_INSTITUTIONAL_PATTERN = (
    r'('
    r'\m(trust|trustee|estate\s+of)\M'
    r'|\m(ministry|ministries|congregation|diocese|temple|mosque|synagogue)\M'
    r'|\m(baptist|methodist|episcopal|presbyterian|catholic|apostolic|united|first|second|new|tabernacle|gospel|evangelical)\s+church\M'
    r'|\mchurch\s+of\M'
    r'|\mchurch\s+inc\M'
    r'|church$'
    r'|salvation\s+army|habitat\s+for\s+humanity'
    r'|cemetery'
    r'|atlanta\s+neighborhood\s+development'
    r')'
)

TABLES = [
    ("fulton_parcels", "owner", None),
    ("dekalb_parcels", "ownernme1", "ownernme2"),
]


def flag_owners(engine):
    with engine.begin() as conn:
        for table, col1, col2 in TABLES:
            print(f"\n--- {table} ---")
            for flag in ("is_corporate", "is_institutional"):
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {flag} BOOLEAN DEFAULT FALSE;"))
                conn.execute(text(f"UPDATE {table} SET {flag} = FALSE;"))

            # 1. Strong Institutional (gov, utilities, schools)
            pattern = STRONG_INSTITUTIONAL_PATTERN
            conn.execute(text(f"UPDATE {table} SET is_institutional = TRUE WHERE {col1} ~* :pattern"), {"pattern": pattern})
            if col2:
                conn.execute(text(f"UPDATE {table} SET is_institutional = TRUE WHERE NOT is_institutional AND {col2} ~* :pattern"), {"pattern": pattern})

            # 2. Medium Institutional (HOAs, Associations) - Checked BEFORE corporate
            pattern = MEDIUM_INSTITUTIONAL_PATTERN
            conn.execute(text(f"UPDATE {table} SET is_institutional = TRUE WHERE NOT is_institutional AND {col1} ~* :pattern"), {"pattern": pattern})
            if col2:
                conn.execute(text(f"UPDATE {table} SET is_institutional = TRUE WHERE NOT is_institutional AND {col2} ~* :pattern"), {"pattern": pattern})

            # 3. Institutional via Land Use (Common Areas)
            if table == "fulton_parcels":
                conn.execute(text("UPDATE fulton_parcels SET is_institutional = TRUE WHERE NOT is_institutional AND \"lucode\" IN ('111', '166', '188', '208')"))
            elif table == "dekalb_parcels":
                conn.execute(text("UPDATE dekalb_parcels SET is_institutional = TRUE WHERE NOT is_institutional AND (\"classcd\" = 'R9' OR \"landuse\" = 'COS' OR \"common_area\" IS NOT NULL)"))

            # 4. Corporate (LLC, Inc, etc) - Only if not already institutional
            pattern = CORPORATE_PATTERN
            conn.execute(text(f"UPDATE {table} SET is_corporate = TRUE WHERE NOT is_institutional AND {col1} ~* :pattern"), {"pattern": pattern})
            if col2:
                conn.execute(text(f"UPDATE {table} SET is_corporate = TRUE WHERE NOT is_institutional AND NOT is_corporate AND {col2} ~* :pattern"), {"pattern": pattern})

            # 5. Weak Institutional (Religious, Trusts) - Only if not corporate
            pattern = WEAK_INSTITUTIONAL_PATTERN
            conn.execute(text(f"UPDATE {table} SET is_institutional = TRUE WHERE NOT is_institutional AND NOT is_corporate AND {col1} ~* :pattern"), {"pattern": pattern})
            if col2:
                conn.execute(text(f"UPDATE {table} SET is_institutional = TRUE WHERE NOT is_institutional AND NOT is_corporate AND {col2} ~* :pattern"), {"pattern": pattern})


    # Summary
    with engine.connect() as conn:
        print("\n=== Summary ===")
        for table, _, _ in TABLES:
            total = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            corp = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE is_corporate")).scalar()
            inst = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE is_institutional")).scalar()
            indiv = total - corp - inst
            print(f"  {table}: {total:,} total — {corp:,} corporate ({100*corp/total:.1f}%), "
                  f"{inst:,} institutional ({100*inst/total:.1f}%), {indiv:,} individual ({100*indiv/total:.1f}%)")

        for label, table, col in [("Fulton", "fulton_parcels", "owner"), ("DeKalb", "dekalb_parcels", "ownernme1")]:
            print(f"\nTop 15 corporate owners ({label}):")
            rows = conn.execute(text(f"""
                SELECT {col}, COUNT(*) as n FROM {table}
                WHERE is_corporate GROUP BY {col} ORDER BY n DESC LIMIT 15
            """)).fetchall()
            for row in rows:
                print(f"  {row.n:>5}  {row[0]}")

            print(f"\nTop 15 institutional owners ({label}):")
            rows = conn.execute(text(f"""
                SELECT {col}, COUNT(*) as n FROM {table}
                WHERE is_institutional GROUP BY {col} ORDER BY n DESC LIMIT 15
            """)).fetchall()
            for row in rows:
                print(f"  {row.n:>5}  {row[0]}")

        # Check: top unflagged multi-parcel owners (potential gaps)
        print("\n=== Top unflagged owners with 5+ parcels (potential gaps) ===")
        for label, table, col in [("Fulton", "fulton_parcels", "owner"), ("DeKalb", "dekalb_parcels", "ownernme1")]:
            print(f"\n{label}:")
            rows = conn.execute(text(f"""
                SELECT {col}, COUNT(*) as n FROM {table}
                WHERE NOT is_corporate AND NOT is_institutional
                GROUP BY {col} HAVING COUNT(*) >= 5
                ORDER BY n DESC LIMIT 20
            """)).fetchall()
            for row in rows:
                print(f"  {row.n:>5}  {row[0]}")


if __name__ == "__main__":
    flag_owners(engine)
