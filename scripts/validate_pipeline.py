#!/usr/bin/env python3
"""
validate_pipeline.py — Post-pipeline sanity checks.

Encodes the benchmarks and structural invariants established during clustering
refinement (planning/12, planning/13, planning/14). Exits nonzero if any
assertion fails.

Run after scripts 10b and before rebuilding materialized views:

    uv run scripts/10b_cluster_refinement.py
    uv run scripts/validate_pipeline.py   # <-- here
    PGPASSWORD=woa psql ... -f scripts/sql/04_create_materialized_views.sql

Can also be run standalone against a live DB at any time.
"""

import sys
from sqlalchemy import create_engine, text

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
engine = create_engine(DB_URL)

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"


def check(label, actual, op, threshold):
    ops = {
        ">=": lambda a, t: a >= t,
        "<=": lambda a, t: a <= t,
        "==": lambda a, t: a == t,
        ">":  lambda a, t: a > t,
        "<":  lambda a, t: a < t,
    }
    ok = ops[op](actual, threshold)
    status = PASS if ok else FAIL
    print(f"  [{status}] {label}: {actual} {op} {threshold}")
    return ok


def sql_scalar(conn, query, params=None):
    row = conn.execute(text(query), params or {}).fetchone()
    return row[0] if row else None


def run_checks():
    failures = 0
    warnings = 0

    with engine.connect() as conn:

        # ------------------------------------------------------------------
        # 1. Structural health
        # ------------------------------------------------------------------
        print("\n--- 1. Structural health ---")

        total_clusters = sql_scalar(conn,
            "SELECT COUNT(*) FROM ownership_clusters")
        ok = check("Total clusters > 400,000", total_clusters, ">=", 400_000)
        if not ok: failures += 1

        total_parcels = sql_scalar(conn,
            "SELECT SUM(parcel_count) FROM ownership_clusters")
        ok = check("Total tracked parcels > 500,000", total_parcels, ">=", 500_000)
        if not ok: failures += 1

        largest = sql_scalar(conn,
            "SELECT MAX(parcel_count) FROM ownership_clusters")
        ok = check("Largest cluster <= 5,000 parcels", largest, "<=", 5_000)
        if not ok: failures += 1

        mega = sql_scalar(conn,
            "SELECT COUNT(*) FROM ownership_clusters WHERE parcel_count > 10000")
        ok = check("No cluster > 10,000 parcels", mega, "==", 0)
        if not ok: failures += 1

        # ------------------------------------------------------------------
        # 2. Known firm benchmarks (Fulton + DeKalb, 2-county subset)
        # ------------------------------------------------------------------
        print("\n--- 2. Known firm benchmarks ---")

        def firm_parcels(name_fragment):
            return sql_scalar(conn, """
                SELECT SUM(count) FROM owner_entities
                WHERE owner_name_norm ILIKE :frag
            """, {"frag": f"%{name_fragment}%"})

        def firm_cluster_count(name_fragment):
            return sql_scalar(conn, """
                SELECT COUNT(DISTINCT cluster_id) FROM owner_entities
                WHERE owner_name_norm ILIKE :frag
            """, {"frag": f"%{name_fragment}%"})

        # Invitation Homes — IH BORROWER / SFR XII / STAR BORROWER series
        ih_parcels = sql_scalar(conn, """
            SELECT SUM(count) FROM owner_entities
            WHERE owner_name_norm ILIKE '%IH BORROWER%'
               OR owner_name_norm ILIKE '%SFR XII%'
               OR owner_name_norm ILIKE '%STAR BORROWER%'
               OR owner_name_norm ILIKE '%STAR 2021 SFR%'
               OR owner_name_norm ILIKE '%STAR 2022 SFR%'
               OR owner_name_norm ILIKE '%TBR SFR%'
        """)
        ok = check("Invitation Homes total parcels >= 2,500", ih_parcels, ">=", 2_500)
        if not ok: failures += 1

        ih_clusters = sql_scalar(conn, """
            SELECT COUNT(DISTINCT cluster_id) FROM owner_entities
            WHERE owner_name_norm ILIKE '%IH BORROWER%'
               OR owner_name_norm ILIKE '%SFR XII%'
               OR owner_name_norm ILIKE '%STAR BORROWER%'
               OR owner_name_norm ILIKE '%TBR SFR%'
        """)
        # IH fragmentation is a known structural limit (STREET_ENTITY_LIMIT gating
        # of the Tustin/Santa Ana addresses). Treat as warning, not failure.
        ih_ok = ih_clusters <= 3
        ih_status = PASS if ih_ok else WARN
        print(f"  [{ih_status}] Invitation Homes series in <= 3 clusters: {ih_clusters}"
              + ("" if ih_ok else " (known fragmentation — see planning/14)"))

        # Progress Residential
        pr_parcels = sql_scalar(conn, """
            SELECT SUM(count) FROM owner_entities
            WHERE owner_name_norm ILIKE '%PROGRESS RESIDENTIAL BORROWER%'
        """)
        ok = check("Progress Residential total parcels >= 500", pr_parcels, ">=", 500)
        if not ok: failures += 1

        # Amherst (BAF ASSETS / ALTO ASSET / CPI AMHERST)
        amherst_parcels = sql_scalar(conn, """
            SELECT SUM(count) FROM owner_entities
            WHERE owner_name_norm ILIKE '%BAF ASSETS%'
               OR owner_name_norm ILIKE '%ALTO ASSET%'
               OR owner_name_norm ILIKE '%CPI AMHERST%'
               OR owner_name_norm ILIKE '%SRMZ%'
               OR owner_name_norm ILIKE '%RH PARTNERS OWNERCO%'
        """)
        ok = check("Amherst total parcels >= 300", amherst_parcels, ">=", 300)
        if not ok: failures += 1

        # Pretium / FYR SFR — must be SEPARATE from Amherst
        fyr_cluster = sql_scalar(conn, """
            SELECT cluster_id FROM owner_entities
            WHERE owner_name_norm ILIKE '%FYR SFR BORROWER%'
            GROUP BY cluster_id ORDER BY SUM(count) DESC LIMIT 1
        """)
        baf_cluster = sql_scalar(conn, """
            SELECT cluster_id FROM owner_entities
            WHERE owner_name_norm ILIKE '%BAF ASSETS%'
            GROUP BY cluster_id ORDER BY SUM(count) DESC LIMIT 1
        """)
        if fyr_cluster is not None and baf_cluster is not None:
            ok = fyr_cluster != baf_cluster
            status = PASS if ok else FAIL
            print(f"  [{status}] Pretium (FYR SFR) and Amherst (BAF ASSETS) in separate clusters"
                  f": {fyr_cluster} vs {baf_cluster}")
            if not ok: failures += 1
        else:
            print(f"  [{WARN}] Could not locate FYR SFR or BAF ASSETS clusters (missing data?)")
            warnings += 1

        # FirstKey Homes
        fkh_parcels = sql_scalar(conn, """
            SELECT SUM(count) FROM owner_entities
            WHERE owner_name_norm ILIKE '%FKH SFR%'
        """)
        ok = check("FirstKey Homes total parcels >= 500", fkh_parcels, ">=", 500)
        if not ok: failures += 1

        # ------------------------------------------------------------------
        # 3. Blocklist effectiveness
        # ------------------------------------------------------------------
        print("\n--- 3. Blocklist effectiveness ---")

        # TAMARIND REEF should not create address edges — check that it does not
        # appear as the SOLE link between two different named-firm families.
        # Proxy: Amherst names and Pretium names should not share a cluster.
        home_sfr_cluster = sql_scalar(conn, """
            SELECT cluster_id FROM owner_entities
            WHERE owner_name_norm ILIKE '%HOME SFR BORROWER%'
            GROUP BY cluster_id ORDER BY SUM(count) DESC LIMIT 1
        """)
        if home_sfr_cluster is not None and fyr_cluster is not None:
            ok = home_sfr_cluster != fyr_cluster
            status = PASS if ok else FAIL
            print(f"  [{status}] HOME SFR BORROWER (Amherst) not merged with FYR SFR BORROWER (Pretium)"
                  f": clusters {home_sfr_cluster} vs {fyr_cluster}")
            if not ok: failures += 1
        else:
            print(f"  [{WARN}] Cannot check Amherst/Pretium separation (cluster lookup failed)")
            warnings += 1

        # KOGER BLVD should not create a bridge either (same two firms)
        # Same check covers it — if FYR and HOME SFR are separate, Koger is clean.

        # ------------------------------------------------------------------
        # 4. Institutional isolation
        # ------------------------------------------------------------------
        print("\n--- 4. Institutional isolation ---")

        # No institutional entity should appear in a large SOS-matched cluster
        # (proxy: clusters where >50% of entities have a sos_control_number)
        # owner_entities has is_institutional but not is_corporate; use
        # sos_control_number IS NOT NULL as the SOS-resolved (corporate) signal.
        inst_in_corp_clusters = sql_scalar(conn, """
            SELECT COUNT(*) FROM owner_entities oe
            JOIN ownership_clusters oc USING (cluster_id)
            WHERE oe.is_institutional = TRUE
              AND oc.parcel_count >= 100
              AND (
                SELECT COUNT(*) FROM owner_entities oe2
                WHERE oe2.cluster_id = oc.cluster_id
                  AND oe2.sos_control_number IS NOT NULL
              ) > (
                SELECT COUNT(*) FROM owner_entities oe3
                WHERE oe3.cluster_id = oc.cluster_id
              ) / 2
        """)
        ok = check("Institutional entities in large corporate clusters == 0",
                   inst_in_corp_clusters, "==", 0)
        if not ok:
            warnings += 1  # warn not fail — mixed clusters can be legitimate

        # ------------------------------------------------------------------
        # 5. Script consistency checks
        # ------------------------------------------------------------------
        print("\n--- 5. Script consistency ---")

        def check_import(path, symbol):
            try:
                src = open(path).read()
                return f"from utils_clustering import" in src and symbol in src
            except FileNotFoundError:
                return False

        # Verify that both scripts are using the shared utility module
        ok_04 = check_import("scripts/04_ownership_network.py", "ADDRESS_STREET_BLOCKLIST")
        ok_10 = check_import("scripts/10_sos_network_enrichment.py", "ADDRESS_STREET_BLOCKLIST")

        if ok_04:
            print(f"  [{PASS}] scripts/04_ownership_network.py imports from utils_clustering")
        else:
            print(f"  [{FAIL}] scripts/04_ownership_network.py is missing utils_clustering import")
            failures += 1

        if ok_10:
            print(f"  [{PASS}] scripts/10_sos_network_enrichment.py imports from utils_clustering")
        else:
            print(f"  [{FAIL}] scripts/10_sos_network_enrichment.py is missing utils_clustering import")
            failures += 1

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        print(f"\n{'='*50}")
        if failures == 0 and warnings == 0:
            print(f"  All checks passed.")
        elif failures == 0:
            print(f"  {warnings} warning(s), 0 failures.")
        else:
            print(f"  {failures} FAILURE(s), {warnings} warning(s).")
        print(f"{'='*50}\n")

    return failures


if __name__ == "__main__":
    failures = run_checks()
    sys.exit(1 if failures > 0 else 0)
