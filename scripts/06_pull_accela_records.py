"""Pull Accela permit records by type and date range.

Searches the Atlanta Accela API for records of a configurable type,
chunked by month, with full expand (addresses, parcels, contacts, etc.)
and workflow history. Upserts into application.records.

Usage:
    # Full backfill of building complaints since 2020
    uv run python scripts/06_pull_accela_records.py

    # Custom type and date range
    uv run python scripts/06_pull_accela_records.py \
        --type "Building/Complaint/NA/NA" \
        --from-date 2024-01-01 --to-date 2024-06-30

    # Pull recently updated records
    uv run python scripts/06_pull_accela_records.py --mode updated \
        --from-date 2026-02-01 --to-date 2026-02-18

Reference: nbh_accela/accela_multi_puller.py (pull_all_ranges pattern)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, date, timedelta
from calendar import monthrange

import psycopg2
from accela import AccelaClient, get_access_token
from dotenv import load_dotenv

load_dotenv()

# --- DB config (who_owns_atl) ---
DB_HOST = os.getenv("WOA_DB_HOST", "localhost")
DB_PORT = os.getenv("WOA_DB_PORT", "5434")
DB_USER = os.getenv("WOA_DB_USER", "woa")
DB_PASS = os.getenv("WOA_DB_PASSWORD", "woa")
DB_NAME = os.getenv("WOA_DB_NAME", "who_owns_atl")

# --- Accela client state ---
_cached_client = None
_token_time = 0

PAGE_SIZE = 100
EXPAND_FIELDS = [
    "addresses", "parcels", "professionals", "contacts",
    "owners", "customForms", "customTables", "assets", "workflows"
]


def log(msg):
    ts = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{ts} {msg}", flush=True)


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASS, dbname=DB_NAME
    )


def get_client(force_refresh=False):
    """Get or refresh Accela API client (token refreshes every 10 min)."""
    global _cached_client, _token_time

    now = time.time()
    if _cached_client and not force_refresh and (now - _token_time < 600):
        return _cached_client

    log("Refreshing Accela API token...")
    scope = " ".join([
        "agencies", "announcements", "app_data", "conditions", "contacts",
        "documents", "gis", "inspections", "owners", "parcels", "payments",
        "professionals", "records", "global_search", "workflows",
    ])

    token = get_access_token(
        client_id=os.getenv("CLIENT_ID"),
        client_secret=os.getenv("CLIENT_SECRET"),
        username=os.getenv("ACCELA_USERNAME"),
        password=os.getenv("ACCELA_PASSWORD"),
        agency_name=os.getenv("AGENCY_NAME", "ATLANTA_GA"),
        environment=os.getenv("ENVIRONMENT", "PROD"),
        grant_type="password",
        scope=scope,
    )

    _cached_client = AccelaClient(
        access_token=token.access_token,
        agency=os.getenv("AGENCY_NAME", "ATLANTA_GA"),
        environment=os.getenv("ENVIRONMENT", "PROD"),
    )
    _token_time = now
    return _cached_client


def parse_record_type(type_str):
    """Parse 'Group/Type/SubType/Category' into recordTypeModel dict."""
    parts = type_str.split("/")
    if len(parts) != 4:
        raise ValueError(f"Record type must be Group/Type/SubType/Category, got: {type_str}")
    return {
        "group": parts[0],
        "type": parts[1],
        "subType": parts[2],
        "category": parts[3],
    }


def generate_monthly_ranges(from_date, to_date):
    """Generate (start, end) date pairs for each calendar month in the range."""
    ranges = []
    current = from_date.replace(day=1)
    while current <= to_date:
        month_end_day = monthrange(current.year, current.month)[1]
        month_end = current.replace(day=month_end_day)
        # Clamp to the actual date range
        start = max(current, from_date)
        end = min(month_end, to_date)
        ranges.append((start, end))
        # Move to first of next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1, day=1)
        else:
            current = current.replace(month=current.month + 1, day=1)
    return ranges


def parse_accela_date(date_str):
    """Parse Accela date string to datetime."""
    if not date_str or not isinstance(date_str, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def calculate_last_action(data):
    """Recursively find the most recent relevant date in the record.

    Checks: markedDate, resultDate, statusDate, lastModifiedDate
    Returns: (newest_date, newest_node_dict)
    """
    newest_date = None
    newest_info = None
    target_keys = {"markedDate", "resultDate", "statusDate", "lastModifiedDate"}

    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in target_keys:
                if key in node and isinstance(node[key], str):
                    dt = parse_accela_date(node[key])
                    if dt and (newest_date is None or dt > newest_date):
                        newest_date = dt
                        newest_info = node
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)

    return newest_date, newest_info


def upsert_record(cur, record_data):
    """Upsert a single record into application.records."""
    accela_id = record_data.get("id")
    permit_number = record_data.get("customId")
    description = record_data.get("description")
    opened_date = parse_accela_date(record_data.get("openedDate"))
    last_action_date, last_action_info = calculate_last_action(record_data)
    status = None
    if isinstance(record_data.get("status"), dict):
        status = record_data["status"].get("text")

    if not accela_id:
        log(f"  Skipping record without ID: {permit_number}")
        return False

    sql = """
        INSERT INTO application.records (
            accela_id, permit_number, opened_date, last_action_date,
            last_action_info, description, status, raw_data, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, NOW()
        )
        ON CONFLICT (accela_id) DO UPDATE SET
            permit_number = EXCLUDED.permit_number,
            opened_date = EXCLUDED.opened_date,
            last_action_date = EXCLUDED.last_action_date,
            last_action_info = EXCLUDED.last_action_info,
            description = EXCLUDED.description,
            status = EXCLUDED.status,
            raw_data = EXCLUDED.raw_data,
            updated_at = NOW()
    """

    cur.execute(sql, (
        accela_id,
        permit_number,
        opened_date,
        last_action_date,
        json.dumps(last_action_info) if last_action_info else None,
        description,
        status,
        json.dumps(record_data),
    ))
    return True


def pull_month(record_type_obj, date_from, date_to, mode="opened"):
    """Pull all records for a single month chunk.

    Args:
        record_type_obj: dict with group/type/subType/category
        date_from: start date string (YYYY-MM-DD)
        date_to: end date string (YYYY-MM-DD)
        mode: "opened" or "updated"

    Returns:
        (new_count, total_seen) tuple
    """
    if mode == "opened":
        search_query = {
            "type": record_type_obj,
            "openedDateFrom": date_from,
            "openedDateTo": date_to,
        }
    else:
        search_query = {
            "type": record_type_obj,
            "updateDateFrom": date_from,
            "updateDateTo": date_to,
        }

    all_records = {}
    offset = 0
    conn = get_db_connection()
    conn.autocommit = True
    cur = conn.cursor()

    try:
        while True:
            client = get_client()
            try:
                results = client.records.search(
                    search_query,
                    limit=PAGE_SIZE,
                    offset=offset,
                    expand=EXPAND_FIELDS,
                )
            except Exception as e:
                err_str = str(e)
                if "401" in err_str or "expired" in err_str.lower():
                    log("  Token expired, refreshing...")
                    get_client(force_refresh=True)
                    continue
                if "404" in err_str or "No record found" in err_str:
                    # No records for this date range — not an error
                    break
                log(f"  API error at offset {offset}: {e}")
                break

            if not results or not results.data:
                break

            new_in_page = 0
            for record in results:
                rec_id = record.id
                if rec_id not in all_records:
                    raw_data = record.raw_json

                    # Fetch workflow history
                    try:
                        histories = client.record_workflow_task_histories.list(rec_id)
                        raw_data["workflow_histories"] = [h.raw_json for h in histories] if histories else []
                    except Exception as whe:
                        raw_data["workflow_histories"] = []

                    # Upsert to DB
                    try:
                        upsert_record(cur, raw_data)
                    except Exception as db_err:
                        log(f"  DB error upserting {raw_data.get('customId')}: {db_err}")
                        conn.rollback()

                    all_records[rec_id] = True
                    new_in_page += 1

            log(f"  Page {offset // PAGE_SIZE + 1}: {len(results.data)} records ({new_in_page} new). has_more={results.has_more}")

            if not results.has_more:
                break
            offset += PAGE_SIZE
    finally:
        cur.close()
        conn.close()

    return len(all_records)


def main():
    parser = argparse.ArgumentParser(
        description="Pull Accela permit records by type and date range"
    )
    parser.add_argument(
        "--type", default="Building/Complaint/NA/NA",
        help="Record type as Group/Type/SubType/Category (default: Building/Complaint/NA/NA)"
    )
    parser.add_argument(
        "--from-date", default="2020-01-01",
        help="Start date YYYY-MM-DD (default: 2020-01-01)"
    )
    parser.add_argument(
        "--to-date", default=datetime.now().strftime("%Y-%m-%d"),
        help="End date YYYY-MM-DD (default: today)"
    )
    parser.add_argument(
        "--mode", choices=["opened", "updated", "both"], default="opened",
        help="Search by opened date, updated date, or both (default: opened)"
    )
    args = parser.parse_args()

    record_type_obj = parse_record_type(args.type)
    from_date = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    to_date = datetime.strptime(args.to_date, "%Y-%m-%d").date()

    log(f"Record type: {args.type}")
    log(f"Date range: {from_date} to {to_date}")
    log(f"Mode: {args.mode}")

    monthly_ranges = generate_monthly_ranges(from_date, to_date)
    log(f"Split into {len(monthly_ranges)} monthly chunks")

    modes = []
    if args.mode in ("opened", "both"):
        modes.append("opened")
    if args.mode in ("updated", "both"):
        modes.append("updated")

    grand_total = 0
    for mode in modes:
        log(f"\n=== Mode: {mode} ===")
        for i, (m_start, m_end) in enumerate(monthly_ranges, 1):
            date_from_str = m_start.strftime("%Y-%m-%d")
            date_to_str = m_end.strftime("%Y-%m-%d")
            log(f"\n--- Month {i}/{len(monthly_ranges)}: {date_from_str} to {date_to_str} ({mode}) ---")

            count = pull_month(record_type_obj, date_from_str, date_to_str, mode=mode)
            grand_total += count
            log(f"  => {count} new records this month")

    log(f"\n=== Complete: {grand_total} total records pulled ===")


if __name__ == "__main__":
    main()
