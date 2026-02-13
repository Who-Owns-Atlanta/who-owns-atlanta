"""Scrape GA Secretary of State business records for corporate owners.

Converted from scrapeSOS.js — uses Playwright instead of Puppeteer.
Stores results in PostGIS tables: ga_business, ga_business_officer, parcel_business.
"""

import argparse
import json
import logging
import time

from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, text

DB_URL = "postgresql://woa:woa@localhost:5434/who_owns_atl"
LIBPOSTAL_URL = "http://localhost:6789"
SOS_URL = "https://ecorp.sos.ga.gov/BusinessSearch"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("sos")

engine = create_engine(DB_URL)


def setup_tables(engine):
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ga_business (
                control_number VARCHAR(20) PRIMARY KEY,
                business_name TEXT,
                business_type TEXT,
                business_status TEXT,
                business_purpose TEXT,
                principal_office_address TEXT,
                registration_date TEXT,
                state_of_formation TEXT,
                last_annual_registration_year TEXT,
                registered_agent_name TEXT,
                registered_agent_address TEXT,
                registered_agent_county TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ga_business_officer (
                id SERIAL PRIMARY KEY,
                control_number VARCHAR(20) REFERENCES ga_business(control_number),
                name TEXT,
                title TEXT,
                business_address TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS parcel_business (
                control_number VARCHAR(20),
                parcel_id TEXT,
                county TEXT,
                notes TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (control_number, parcel_id)
            );
        """))


def get_pending_lookups(engine, limit=None):
    """Get corporate owner names that haven't been looked up yet."""
    q = """
        SELECT DISTINCT owner_name_norm
        FROM owner_entities oe
        WHERE oe.owner_name_norm IN (
            SELECT UPPER(TRIM(owner_name)) FROM parcels_unified WHERE is_corporate
        )
        AND NOT EXISTS (
            SELECT 1 FROM parcel_business pb
            WHERE pb.notes IS NOT NULL
            AND pb.parcel_id IN (SELECT UNNEST(oe.parcel_ids))
        )
        ORDER BY owner_name_norm
    """
    if limit:
        q += f" LIMIT {limit}"
    with engine.connect() as conn:
        return [r.owner_name_norm for r in conn.execute(text(q)).fetchall()]


def get_search_results(page):
    """Extract search results from the results table."""
    try:
        page.wait_for_selector("#grid_businessList tbody tr, #errorDialog", timeout=30000)
    except Exception:
        log.warning("Timeout waiting for search results")
        page.screenshot(path="/tmp/sos_debug.png")
        log.info("Debug screenshot saved to /tmp/sos_debug.png")
        return []

    # Check for "No data found" dialog
    no_data = page.evaluate("""() => {
        const dialog = document.querySelector('#errorDialog');
        if (dialog) {
            const msg = dialog.querySelector('.error_message');
            return msg && msg.textContent.includes('No data found');
        }
        return false;
    }""")

    if no_data:
        try:
            page.click(".ui-dialog-buttonset button")
        except Exception:
            pass
        return []

    results = page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll('#grid_businessList tbody tr'));
        return rows.map(row => ({
            businessName: row.querySelector('td:nth-child(1) a')?.textContent.trim() || '',
            controlNumber: row.querySelector('td:nth-child(2)')?.textContent.trim() || '',
            businessType: row.querySelector('td:nth-child(3)')?.textContent.trim() || '',
            principalOfficeAddress: row.querySelector('td:nth-child(4)')?.textContent.trim() || '',
            status: row.querySelector('td:nth-child(6)')?.textContent.trim() || '',
            url: row.querySelector('td:nth-child(1) a')?.href || '',
        }));
    }""")

    # Filter out name reservations and empty control numbers
    return [r for r in results if r["businessType"] != "Name Reservation" and r["controlNumber"].strip()]


def scrape_business_detail(page):
    """Scrape the business detail page."""
    info = page.evaluate("""() => {
        const getInfoByLabel = (label) => {
            const allTds = Array.from(document.querySelectorAll('tr > td'));
            const labelTd = allTds.find(td => td.textContent.trim() === label);
            if (labelTd) {
                const valueTd = labelTd.nextElementSibling;
                if (valueTd) {
                    const strong = valueTd.querySelector('strong');
                    return strong ? strong.textContent.trim() : null;
                }
            }
            return null;
        };

        const officers = Array.from(
            document.querySelectorAll('#grid_principalList tbody tr')
        ).map(row => ({
            name: row.querySelector('td:nth-child(1)')?.textContent.trim() || '',
            title: row.querySelector('td:nth-child(2)')?.textContent.trim() || '',
            business_address: row.querySelector('td:nth-child(3)')?.textContent.trim() || '',
        }));

        return {
            business_name: getInfoByLabel('Business Name:'),
            control_number: getInfoByLabel('Control Number:'),
            business_type: getInfoByLabel('Business Type:'),
            business_status: getInfoByLabel('Business Status:'),
            business_purpose: getInfoByLabel('Business Purpose:'),
            principal_office_address: getInfoByLabel('Principal Office Address:'),
            registration_date: getInfoByLabel('Date of Formation / Registration Date:'),
            state_of_formation: getInfoByLabel('State of Formation:'),
            last_annual_registration_year: getInfoByLabel('Last Annual Registration Year:'),
            registered_agent_name: getInfoByLabel('Registered Agent Name:'),
            registered_agent_address: getInfoByLabel('Physical Address:'),
            registered_agent_county: getInfoByLabel('County:'),
            officers: officers,
        };
    }""")
    return info


def save_business(engine, info, parcel_ids=None, county=None):
    """Save business info and officer data to the database."""
    officers = info.pop("officers", [])
    control_number = info.get("control_number")
    if not control_number:
        return

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ga_business (
                control_number, business_name, business_type, business_status,
                business_purpose, principal_office_address, registration_date,
                state_of_formation, last_annual_registration_year,
                registered_agent_name, registered_agent_address, registered_agent_county
            ) VALUES (
                :control_number, :business_name, :business_type, :business_status,
                :business_purpose, :principal_office_address, :registration_date,
                :state_of_formation, :last_annual_registration_year,
                :registered_agent_name, :registered_agent_address, :registered_agent_county
            )
            ON CONFLICT (control_number) DO UPDATE SET
                business_name=EXCLUDED.business_name,
                business_type=EXCLUDED.business_type,
                business_status=EXCLUDED.business_status,
                business_purpose=EXCLUDED.business_purpose,
                principal_office_address=EXCLUDED.principal_office_address,
                registration_date=EXCLUDED.registration_date,
                state_of_formation=EXCLUDED.state_of_formation,
                last_annual_registration_year=EXCLUDED.last_annual_registration_year,
                registered_agent_name=EXCLUDED.registered_agent_name,
                registered_agent_address=EXCLUDED.registered_agent_address,
                registered_agent_county=EXCLUDED.registered_agent_county,
                updated_at=NOW();
        """), info)

        for officer in officers:
            conn.execute(text("""
                INSERT INTO ga_business_officer (control_number, name, title, business_address)
                VALUES (:control_number, :name, :title, :business_address)
            """), {"control_number": control_number, **officer})

        if parcel_ids:
            for pid in parcel_ids:
                conn.execute(text("""
                    INSERT INTO parcel_business (control_number, parcel_id, county, notes)
                    VALUES (:cn, :pid, :county, 'matched')
                    ON CONFLICT (control_number, parcel_id) DO UPDATE SET
                        notes='matched', updated_at=NOW();
                """), {"cn": control_number, "pid": pid, "county": county})


def log_search_outcome(engine, owner_name, notes, parcel_ids=None, county=None):
    """Log a search that didn't result in a match."""
    with engine.begin() as conn:
        if parcel_ids:
            for pid in parcel_ids:
                conn.execute(text("""
                    INSERT INTO parcel_business (control_number, parcel_id, county, notes)
                    VALUES (:cn, :pid, :county, :notes)
                    ON CONFLICT (control_number, parcel_id) DO UPDATE SET
                        notes=EXCLUDED.notes, updated_at=NOW();
                """), {"cn": f"SEARCH_{owner_name[:20]}", "pid": pid, "county": county, "notes": notes})


def search_business(page, engine, owner_name, parcel_ids=None, county=None):
    """Search SOS for a business name and scrape results."""
    log.info(f"Searching: {owner_name}")

    try:
        page.goto(SOS_URL, wait_until="networkidle", timeout=60000)
    except Exception as e:
        log.error(f"Failed to load search page: {e}")
        log_search_outcome(engine, owner_name, f"Error: page load failed", parcel_ids, county)
        return None

    # Wait for Cloudflare challenge to clear, then for the search form
    try:
        page.wait_for_selector("#rdBusinessName", timeout=30000)
    except Exception:
        log.warning("Search form not found — possibly blocked by Cloudflare")
        page.screenshot(path="/tmp/sos_debug.png")
        log.info("Screenshot saved to /tmp/sos_debug.png")
        log_search_outcome(engine, owner_name, "Error: Cloudflare block", parcel_ids, county)
        return None

    # Search by business name, starts-with first
    page.click("#rdBusinessName")
    page.fill("#txtBusinessName", owner_name)
    page.click("#rdStartsWith")
    page.click("#btnSearch")

    results = get_search_results(page)

    if not results:
        # Try "contains" search
        log.info(f"  No starts-with results, trying contains...")
        try:
            page.goto(SOS_URL, wait_until="networkidle", timeout=60000)
            page.wait_for_selector("#rdBusinessName", timeout=30000)
        except Exception:
            log_search_outcome(engine, owner_name, "Error: page reload failed", parcel_ids, county)
            return None
        page.click("#rdBusinessName")
        page.fill("#txtBusinessName", owner_name)
        page.click("#rdContains")
        page.click("#btnSearch")
        results = get_search_results(page)

    if not results:
        log.info(f"  No results found")
        log_search_outcome(engine, owner_name, "No results", parcel_ids, county)
        return None

    log.info(f"  {len(results)} results")

    # Find best match
    target = None
    norm_search = owner_name.lower().replace(",", "").replace(".", "")

    # Try exact match
    exact = [r for r in results if r["businessName"].lower().replace(",", "").replace(".", "") == norm_search]
    if len(exact) == 1:
        target = exact[0]
    elif len(exact) > 1:
        # Filter by active status
        active = [r for r in exact if r["status"] == "Active/Compliance"]
        if len(active) == 1:
            target = active[0]
        elif active:
            target = active[0]  # Take first active
            log.info(f"  Multiple active exact matches, taking first")
        else:
            target = exact[0]
    else:
        # Try starts-with match
        starts = [r for r in results if r["businessName"].lower().replace(",", "").replace(".", "").startswith(norm_search)]
        if len(starts) == 1:
            target = starts[0]
        elif len(starts) > 1:
            active = [r for r in starts if r["status"] == "Active/Compliance"]
            target = active[0] if active else starts[0]

    if not target:
        log.info(f"  No matching result for {owner_name}")
        log_search_outcome(engine, owner_name, f"No match from {len(results)} results", parcel_ids, county)
        return None

    # Navigate to detail page
    log.info(f"  Match: {target['businessName']} ({target['controlNumber']})")
    try:
        detail_url = target["url"]
        if not detail_url.startswith("http"):
            detail_url = f"https://ecorp.sos.ga.gov{detail_url}"
        page.goto(detail_url, wait_until="networkidle", timeout=60000)
        info = scrape_business_detail(page)
        save_business(engine, info, parcel_ids, county)
        log.info(f"  Saved: {info.get('business_name')} (agent: {info.get('registered_agent_name')})")
        return info
    except Exception as e:
        log.error(f"  Error scraping detail: {e}")
        log_search_outcome(engine, owner_name, f"Error: detail scrape failed", parcel_ids, county)
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", help="Search a single business name")
    parser.add_argument("--limit", type=int, help="Limit number of lookups")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between searches (seconds)")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    args = parser.parse_args()

    setup_tables(engine)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        from playwright_stealth import Stealth
        stealth = Stealth()
        page = context.new_page()
        stealth.apply_stealth_sync(page)

        # Block unnecessary resources
        def handle_route(route):
            if route.request.resource_type in ("image", "stylesheet", "font"):
                route.abort()
            else:
                route.fallback()
        page.route("**/*", handle_route)

        if args.name:
            search_business(page, engine, args.name)
        else:
            names = get_pending_lookups(engine, args.limit)
            log.info(f"{len(names):,} names to look up")
            for i, name in enumerate(names):
                search_business(page, engine, name)
                if i < len(names) - 1:
                    time.sleep(args.delay)
                if (i + 1) % 100 == 0:
                    log.info(f"Progress: {i+1:,} / {len(names):,}")

        browser.close()

    # Summary
    with engine.connect() as conn:
        matched = conn.execute(text("SELECT COUNT(*) FROM ga_business")).scalar()
        log.info(f"Total businesses in DB: {matched:,}")


if __name__ == "__main__":
    main()
