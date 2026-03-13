import re

# --- Tuning knobs ---

# Increased to 100 as institutional noise (MARTA, GA Power) is now institutional-flagged.
NAME_ENTROPY_LIMIT = 100
INDIVIDUAL_NAME_ENTROPY_LIMIT = 5

JUNK_NAME_BLOCKLIST = {
    'RESTRICTED', 'UNKNOWN OWNER', 'CURRENT RESIDENT', 'ESTATE OF', 'UNKNOWN'
}

# Skip addresses if shared by many entities (mailbox centers, office parks)
# Lowered from 50 to 30 to prevent builder-to-buyer bridges while keeping legitimate operators
STREET_ENTITY_LIMIT = 30

# Known corporate developer keywords to trigger the builder-buyer heuristic
BUILDER_KEYWORDS = {'HORTON', 'BROCK', 'PULTE', 'LENNAR', 'CENTURY', 'BEAZER', 'ASHTON', 'MERITAGE', 'TOLL', 'KB HOME'}

# Addresses (matched as prefix of normalize_street output) that must never create
# address edges — multi-firm hub offices where unrelated institutional landlords
# happen to share a mailing address.
# Format: use the street number + name only (no suite, city, state, zip).
ADDRESS_STREET_BLOCKLIST = {
    '3505 KOGER BLVD',     # Duluth GA — Pretium (FYR SFR BORROWER) + Amherst (HOME SFR BORROWER)
    '5100 TAMARIND REEF',  # Christiansted USVI — same two firms share a USVI trust address
    '289 S CULVER ST',     # Lawrenceville GA — Common RA hub for many small LLCs
}

# Substrings that identify commercial registered agents.
# Any agent name containing these will be skipped for edge generation.
COMMERCIAL_RA_SUBSTRINGS = [
    "CORPORATION SERVICE COMPANY", "C T CORPORATION", "CT CORPORATION",
    "COGENCY GLOBAL", "NORTHWEST REGISTERED AGENT", "REGISTERED AGENTS INC",
    "NATIONAL REGISTERED AGENT", "UNITED STATES CORPORATION AGENT",
    "CORPORATE CREATIONS NETWORK", "CSC OF COBB COUNTY", "VCORP AGENT",
    "CAPITOL CORPORATE SERVICES", "INCORP SERVICES", "ANDERSON REGISTERED AGENT", 
    "REPUBLIC REGISTERED AGENT", "ACCESS MANAGEMENT", "LEGALINC CORPORATE", "PARACORP",
    "HOMEOWNER MANAGEMENT", "COMMUNITY MANAGEMENT", "FIELDSTONE REALTY PARTNER",
    "SENTRY MANAGEMENT", "HOMESIDE PROPERTIES", "SILVERLEAF MANAGEMENT",
    "GEORGIA REGISTERED AGENT", "BUSINESS FILINGS INC", "UNIVERSAL REGISTERED AGENT",
    "BCS CORPORATE", "TERRAPIN CORPORATE", "HERITAGE PROPERTY MANAGEMENT",
    "ATLANTA COMMUNITY SERVICE", "BEACON COMMUNITY MANAGEMENT", "BEACON MANAGEMENT",
    "TOLLEY COMMUNITY", "POSOLUTIONS", "CANOPY SERVICE", "SPI AGENT SOLUTIONS",
    "PMI NORTHEAST ATLANTA", "ZENBUSINESS", "REGISTERED AGENT SOLUTIONS",
]

# --- Helper functions ---

_STRIP_PUNCT = re.compile(r'[^A-Z0-9 ]')

def is_builder(name: str) -> bool:
    """Check if owner name contains known builder keywords."""
    if not name: return False
    n = name.upper()
    return any(k in n for k in BUILDER_KEYWORDS)

def normalize_street(addr: str) -> str:
    """Strip Suite/Unit/Apt from address to find the base building."""
    if not addr: return ""
    # Remove junk characters
    s = _STRIP_PUNCT.sub("", addr.upper()).strip()
    # Strip suite/unit
    s = re.sub(r'\s+(STE|SUITE|UNIT|BLDG|OFFICE|#|APT)\s+.*$', '', s, flags=re.IGNORECASE).strip()
    # Normalize common suffixes to improve matching (STREET -> ST, etc)
    s = re.sub(r'\bSTREET\b', 'ST', s)
    s = re.sub(r'\bAVENUE\b', 'AVE', s)
    s = re.sub(r'\bROAD\b', 'RD', s)
    s = re.sub(r'\bDRIVE\b', 'DR', s)
    s = re.sub(r'\bLANE\b', 'LN', s)
    s = re.sub(r'\bCOURT\b', 'CT', s)
    s = re.sub(r'\bBOULEVARD\b', 'BLVD', s)
    s = re.sub(r'\bPLACE\b', 'PL', s)
    s = re.sub(r'\bTERRACE\b', 'TER', s)
    s = re.sub(r'\bPARKWAY\b', 'PKWY', s)
    return s.strip()

def is_commercial_ra(name: str) -> bool:
    """Check if a name identifies a commercial registered agent firm."""
    if not name: return True
    n = _STRIP_PUNCT.sub("", name.upper()).strip()
    if n in ("NONE", "", "LEE MASON", "BILL WETTER"): return True
    return any(sub in n for sub in COMMERCIAL_RA_SUBSTRINGS)

def ra_key(name: str, street: str = "") -> str:
    """Generate a stable key for registered agent matching."""
    if not name: return ""
    name_part = _STRIP_PUNCT.sub("", name.upper()).strip()
    street_part = normalize_street(street)
    return f"{name_part}|{street_part}"
