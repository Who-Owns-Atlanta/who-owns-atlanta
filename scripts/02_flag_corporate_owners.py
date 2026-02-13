"""Flag corporate/institutional owners in both parcel tables."""

from sqlalchemy import create_engine, text

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

CORPORATE_PATTERN = r'\m(llc|inc|corp|ltd|lp|assoc|assn|foundation|company|system|board of regents|department of transportation|plan|pc|venture|ventures|invest|investments|partners)\M'


def flag_owners(engine):
    with engine.begin() as conn:
        # Add is_corporate column to both tables
        for table, col in [("fulton_parcels", "owner"), ("dekalb_parcels", "ownernme1")]:
            print(f"Flagging corporate owners in {table}...")
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS is_corporate BOOLEAN DEFAULT FALSE;"))
            result = conn.execute(text(f"""
                UPDATE {table}
                SET is_corporate = TRUE
                WHERE {col} ~* :pattern
            """), {"pattern": CORPORATE_PATTERN})
            print(f"  Flagged {result.rowcount:,} rows")

        # Also check dekalb ownernme2
        result = conn.execute(text("""
            UPDATE dekalb_parcels
            SET is_corporate = TRUE
            WHERE is_corporate = FALSE
              AND ownernme2 ~* :pattern
        """), {"pattern": CORPORATE_PATTERN})
        print(f"  Flagged {result.rowcount:,} additional DeKalb rows via ownernme2")

    # Summary
    with engine.connect() as conn:
        print("\nCorporate owner summary:")
        for table in ["fulton_parcels", "dekalb_parcels"]:
            total = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            corp = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE is_corporate")).scalar()
            print(f"  {table}: {corp:,} / {total:,} ({100*corp/total:.1f}%)")

        # Show top corporate owners
        print("\nTop 20 corporate owners (Fulton):")
        rows = conn.execute(text("""
            SELECT owner, COUNT(*) as n FROM fulton_parcels
            WHERE is_corporate GROUP BY owner ORDER BY n DESC LIMIT 20
        """)).fetchall()
        for row in rows:
            print(f"  {row.n:>5}  {row.owner}")

        print("\nTop 20 corporate owners (DeKalb):")
        rows = conn.execute(text("""
            SELECT ownernme1, COUNT(*) as n FROM dekalb_parcels
            WHERE is_corporate GROUP BY ownernme1 ORDER BY n DESC LIMIT 20
        """)).fetchall()
        for row in rows:
            print(f"  {row.n:>5}  {row.ownernme1}")


if __name__ == "__main__":
    flag_owners(engine)
