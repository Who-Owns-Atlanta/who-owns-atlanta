"""Side-by-side stat comparison between two named databases.

Usage:
    uv run scripts/compare_releases.py <db_a> <db_b>

Example:
    uv run scripts/compare_releases.py who_owns_atl woa_v202603a1
"""

import sys
from sqlalchemy import create_engine, text

DB_BASE = "postgresql://woa:woa@localhost:5434"

GREEN = "\033[32m"
AMBER = "\033[33m"
RED   = "\033[31m"
RESET = "\033[0m"

def _engine(dbname):
    return create_engine(f"{DB_BASE}/{dbname}")


def _scalar(conn, sql, **params):
    row = conn.execute(text(sql), params).fetchone()
    return row[0] if row else None


def _delta_str(a, b):
    if a is None or b is None:
        return "n/a"
    diff = b - a
    if diff > 0:
        return f"{GREEN}+{diff:,}{RESET}"
    elif diff < 0:
        return f"{AMBER}{diff:,}{RESET}"
    return "0"


def section_summary(eng_a, eng_b, label_a, label_b):
    print(f"\n{'='*70}")
    print(f"  SUMMARY — {label_a}  vs  {label_b}")
    print(f"{'='*70}")
    header = f"{'Metric':<40} {'DB_A':>12} {'DB_B':>12} {'delta':>10}"
    print(header)
    print("-" * 70)

    rows = []
    with eng_a.connect() as ca, eng_b.connect() as cb:
        metrics = [
            ("Fulton parcels",       "SELECT COUNT(*) FROM fulton_parcels"),
            ("DeKalb parcels",       "SELECT COUNT(*) FROM dekalb_parcels"),
            ("Total clusters",       "SELECT COUNT(*) FROM ownership_clusters"),
            ("Max cluster size",     "SELECT MAX(parcel_count) FROM ownership_clusters"),
            ("Avg cluster size",     "SELECT ROUND(AVG(parcel_count),1) FROM ownership_clusters"),
            ("Corporate parcels",    "SELECT COUNT(*) FROM fulton_parcels WHERE is_corporate UNION ALL SELECT COUNT(*) FROM dekalb_parcels WHERE is_corporate"),
            ("Institutional parcels","SELECT COUNT(*) FROM fulton_parcels WHERE is_institutional UNION ALL SELECT COUNT(*) FROM dekalb_parcels WHERE is_institutional"),
            ("Owner entities",       "SELECT COUNT(*) FROM owner_entities"),
            ("SOS-matched entities", "SELECT COUNT(*) FROM owner_entities WHERE sos_match_count > 0"),
        ]

        # Corporate / institutional need summed queries
        corp_sql   = "SELECT SUM(c) FROM (SELECT COUNT(*) c FROM fulton_parcels WHERE is_corporate UNION ALL SELECT COUNT(*) FROM dekalb_parcels WHERE is_corporate) x"
        inst_sql   = "SELECT SUM(c) FROM (SELECT COUNT(*) c FROM fulton_parcels WHERE is_institutional UNION ALL SELECT COUNT(*) FROM dekalb_parcels WHERE is_institutional) x"
        total_sql  = "SELECT SUM(c) FROM (SELECT COUNT(*) c FROM fulton_parcels UNION ALL SELECT COUNT(*) FROM dekalb_parcels) x"

        simple_metrics = [
            ("Fulton parcels",       "SELECT COUNT(*) FROM fulton_parcels"),
            ("DeKalb parcels",       "SELECT COUNT(*) FROM dekalb_parcels"),
            ("Total clusters",       "SELECT COUNT(*) FROM ownership_clusters"),
            ("Max cluster size",     "SELECT MAX(parcel_count) FROM ownership_clusters"),
            ("Avg cluster size",     "SELECT ROUND(AVG(parcel_count)::numeric,1) FROM ownership_clusters"),
            ("Owner entities",       "SELECT COUNT(*) FROM owner_entities"),
            ("SOS-matched entities", "SELECT COUNT(*) FROM owner_entities WHERE sos_control_number IS NOT NULL"),
        ]

        corp_sql  = "SELECT (SELECT COUNT(*) FROM fulton_parcels WHERE is_corporate) + (SELECT COUNT(*) FROM dekalb_parcels WHERE is_corporate)"
        inst_sql  = "SELECT (SELECT COUNT(*) FROM fulton_parcels WHERE is_institutional) + (SELECT COUNT(*) FROM dekalb_parcels WHERE is_institutional)"
        total_sql = "SELECT (SELECT COUNT(*) FROM fulton_parcels) + (SELECT COUNT(*) FROM dekalb_parcels)"

        def run(conn, sql):
            try:
                return conn.execute(text(sql)).scalar()
            except Exception:
                return None

        vals = {}
        for name, sql in simple_metrics:
            vals[name] = (run(ca, sql), run(cb, sql))

        for name, sql in [("Corporate parcels", corp_sql), ("Institutional parcels", inst_sql), ("Total parcels", total_sql)]:
            vals[name] = (run(ca, sql), run(cb, sql))

        ordered = [
            "Total parcels", "Fulton parcels", "DeKalb parcels",
            "Total clusters", "Max cluster size", "Avg cluster size",
            "Corporate parcels", "Institutional parcels",
            "Owner entities", "SOS-matched entities",
        ]

        for name in ordered:
            a_val, b_val = vals[name]
            a_str = f"{a_val:,}" if isinstance(a_val, (int, float)) and a_val is not None else str(a_val or "n/a")
            b_str = f"{b_val:,}" if isinstance(b_val, (int, float)) and b_val is not None else str(b_val or "n/a")
            # Avg cluster size is float, skip comma formatting
            if "Avg" in name:
                a_str = str(a_val or "n/a")
                b_str = str(b_val or "n/a")
            try:
                d = _delta_str(int(a_val) if a_val else None, int(b_val) if b_val else None)
            except Exception:
                d = "n/a"
            print(f"  {name:<38} {a_str:>12} {b_str:>12} {d:>10}")


def section_leaderboard(eng_a, eng_b, label_a, label_b):
    print(f"\n{'='*70}")
    print(f"  TOP-10 CLUSTERS — {label_a}  vs  {label_b}")
    print(f"{'='*70}")

    def get_top10(eng):
        try:
            with eng.connect() as conn:
                rows = conn.execute(text(
                    "SELECT cluster_id, owner_names[1], parcel_count::int FROM mv_leaderboard ORDER BY parcel_count DESC LIMIT 10"
                )).fetchall()
                return rows
        except Exception:
            return []

    top_a = get_top10(eng_a)
    top_b = get_top10(eng_b)

    print(f"  {'#':<3} {'DB_A cluster / owner':<35} {'cnt_a':>6}   {'DB_B cluster / owner':<35} {'cnt_b':>6} {'chg':>4}")
    print("  " + "-" * 95)
    for i in range(10):
        ra = top_a[i] if i < len(top_a) else None
        rb = top_b[i] if i < len(top_b) else None
        a_str = f"{ra[0]} {(ra[1] or '')[:28]}" if ra else ""
        b_str = f"{rb[0]} {(rb[1] or '')[:28]}" if rb else ""
        a_cnt = f"{ra[2]:,}" if ra else ""
        b_cnt = f"{rb[2]:,}" if rb else ""
        changed = ""
        if ra and rb and ra[0] != rb[0]:
            changed = f"{AMBER}!={RESET}"
        print(f"  {i+1:<3} {a_str:<35} {a_cnt:>6}   {b_str:<35} {b_cnt:>6} {changed:>4}")


def section_firm_benchmarks(eng_a, eng_b, label_a, label_b):
    print(f"\n{'='*70}")
    print(f"  FIRM BENCHMARKS — {label_a}  vs  {label_b}")
    print(f"{'='*70}")

    firms = [
        ("Invitation Homes", [
            "%IH BORROWER%", "%SFR XII%", "%STAR BORROWER%",
            "%STAR 2021 SFR%", "%STAR 2022 SFR%", "%TBR SFR%",
        ]),
        ("Progress Residential", ["%PROGRESS RESIDENTIAL BORROWER%"]),
        ("FirstKey Homes",       ["%FKH SFR%"]),
        ("Amherst",              ["%BAF ASSETS%", "%ALTO ASSET%", "%CPI AMHERST%", "%SRMZ%"]),
        ("Pretium (FYR SFR)",    ["%FYR SFR BORROWER%"]),
        ("Home Partners",        ["%HOME SFR BORROWER%"]),
    ]

    def count_firm(eng, patterns):
        try:
            with eng.connect() as conn:
                clauses = " OR ".join(["owner_name_norm ILIKE :p" + str(i) for i in range(len(patterns))])
                params = {f"p{i}": p for i, p in enumerate(patterns)}
                sql = f"SELECT COUNT(DISTINCT cluster_id) FROM owner_entities WHERE {clauses}"
                clusters = conn.execute(text(sql), params).scalar()
                sql2 = f"SELECT COALESCE(SUM(ARRAY_LENGTH(parcel_ids,1)),0) FROM owner_entities WHERE {clauses}"
                parcels = conn.execute(text(sql2), params).scalar()
                return clusters, parcels
        except Exception:
            return None, None

    header = f"  {'Firm':<28} {'clusters_a':>10} {'parcels_a':>10} {'clusters_b':>10} {'parcels_b':>10} {'Δparcels':>10}"
    print(header)
    print("  " + "-" * 82)
    for firm, patterns in firms:
        ca, pa = count_firm(eng_a, patterns)
        cb, pb = count_firm(eng_b, patterns)
        ca_s = f"{ca:,}" if ca is not None else "n/a"
        pa_s = f"{pa:,}" if pa is not None else "n/a"
        cb_s = f"{cb:,}" if cb is not None else "n/a"
        pb_s = f"{pb:,}" if pb is not None else "n/a"
        delta = _delta_str(pa, pb)
        print(f"  {firm:<28} {ca_s:>10} {pa_s:>10} {cb_s:>10} {pb_s:>10} {delta:>10}")


def main():
    if len(sys.argv) != 3:
        print("Usage: compare_releases.py <db_a> <db_b>")
        sys.exit(1)

    db_a, db_b = sys.argv[1], sys.argv[2]
    eng_a = _engine(db_a)
    eng_b = _engine(db_b)

    section_summary(eng_a, eng_b, db_a, db_b)
    section_leaderboard(eng_a, eng_b, db_a, db_b)
    section_firm_benchmarks(eng_a, eng_b, db_a, db_b)

    print(f"\n{'='*70}")
    print(f"  Done.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
