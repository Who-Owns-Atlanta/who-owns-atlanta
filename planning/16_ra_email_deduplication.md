# Plan: Email-Based RA Deduplication (Deferred — For Future Consideration)

## Context

The current clustering pipeline (Pass 2a in `scripts/10_sos_network_enrichment.py`) groups
entities by registered agent using a `ra_key(name, street)` composite. This works when the
same person uses consistent name + address, but fails when they operate under personal name
AND a company name (e.g. "Zephrina Cazaubon" vs "ZMC & Associates LLC") or register from
multiple addresses (e.g. Azeez Khan at 1851 Peeler Rd vs 2028 Luxuria Ct).

The `sos.registered_agents` table has an `email` column (58% populated) that would collapse
these variants to a single identity. This plan describes how to implement it and analyzes
potential harm to existing cluster quality.

---

## How the Current Pipeline Works (Relevant Parts)

**`ra_key()` in `scripts/10_sos_network_enrichment.py` (line 73):**
```python
def ra_key(name: str, street: str = "") -> str:
    name_part = _STRIP_PUNCT.sub("", name.upper()).strip()
    street_part = re.sub(r'\b(STE|SUITE|UNIT|...)\s+.*$', '', ...)
    return f"{name_part}|{street_part}"
```
Groups entities by `(normalized_name | street_without_suite)`. Two RA records only collide
if both name AND street base match. "ZEPHRINA CAZAUBON|3300 BUCKEYE RD" ≠
"ZMC ASSOCIATES LLC|4568 LAWRENCEVILLE HWY" → no edge.

**Pass 2a builds `ra_idx` dict**, then edges all pairs in each key group (capped at
MAX_RA_ENTITIES=500). Commercial RAs blocked by `COMMERCIAL_RA_SKIP` (57 entries).

**Clusters formed by NetworkX `connected_components()`** — not Louvain. Every new edge
transitively merges clusters.

**Pass B fission** (script 10b) can split false-positive merges, but only for clusters
≥ 300 parcels. Below that, bad merges are permanent.

---

## Proposed Implementation

### Step 1: Email blocklist (analogous to COMMERCIAL_RA_SKIP)

Add `EMAIL_RA_SKIP` set of known commercial RA email domains:
```python
EMAIL_RA_SKIP_DOMAINS = {
    "legalzoom.com", "incfile.com", "registeredagentsinc.com",
    "zenbusiness.com", "georgiaregisteredagent.com",
    "northwestregisteredagent.com", "wolterskluwer.com",
    "zmcaccounting.com",  # ZMC Accounting (older firm, different from ZMC Associates)
    # etc.
}
```
Also: if email appears on more than N RA records in the full sos.registered_agents table
(threshold ~50), treat it as commercial and skip. This auto-catches unlisted providers.

### Step 2: New Pass 2a' — "shared_ra_email" edges

Add a second RA pass in `add_ra_edges()` immediately after the existing ra_key pass:

```python
# Build email index from sos.registered_agents JOIN owner_entities
# Group by email where email is valid (not empty, not blocklisted, count < threshold)
ra_email_idx = {}
for row in entities:
    eid, ra_id = row[0], row[5]  # entity_id, sos_registered_agent_id
    email = email_for_ra_id.get(ra_id)   # pre-fetched from sos.registered_agents
    if not email or is_blocked_email(email): continue
    ra_email_idx.setdefault(email, []).append(eid)

# Same edge-building logic as existing Pass 2a
for email, eids in ra_email_idx.items():
    if len(eids) > MAX_RA_ENTITIES: continue
    for u, v in combinations(eids, 2):
        G.add_edge(u, v, rel="shared_ra_email", label=f"RA email: {email}")
```

### Step 3: Fetch email into enrichment (script 09)

In `scripts/09_enrich_owners_sos.py`, add `ra.email` to the JOIN SELECT so it's available
during Pass 2a' without a second DB round-trip. Store it temporarily (no schema change
needed if fetched inline).

### Files to modify
- `scripts/10_sos_network_enrichment.py` — add Pass 2a', email blocklist
- `scripts/09_enrich_owners_sos.py` — optionally surface email for logging/debug
- No schema changes to `owner_entities` required

---

## Impact Analysis

### Current scale
| Metric | Value |
|--------|-------|
| Total owner_entities | 523,496 |
| Entities with RA link (sos_registered_agent_id set) | 36,990 (7.1%) |
| sos.registered_agents rows | 10.3M |
| Rows with real email | 5.9M (58%) |

Only 7% of entities have RA data at all. Email-based deduplication only fires on this subset.

### New edges that would be created

Email creates edges that ra_key() misses only in two cases:

**Case A: Same person, different RA names** (Zephrina Cazaubon / ZMC & Associates LLC)
- Currently: separate ra_key entries → no edge between clients
- With email: `zcazaubon@zmcassociates.com` collapses all ~183 RA IDs → edges between all clients

**Case B: Same person, same name, different address** (Azeez Khan at two addresses)
- Currently: ra_key normalizes suite numbers but NOT base street, so "1851 PEELER RD" ≠
  "2028 LUXURIA CT" → no cross-address edge
- With email: if Azeez Khan uses a consistent email, all 4 RA IDs collapse → edges across addresses

Cases that are NOT new (email adds nothing):
- Same RA name + same base street → ra_key() already catches them
- Entities with no RA data → unaffected (93% of the corpus)
- Commercial RAs → blocked by email domain blocklist

### Cluster size distribution and risk profile

```
Single-parcel:  429,957 clusters (94.5%)   — can only grow, not shrink
2-5 parcel:      34,503 clusters  (7.6%)   — PRIMARY RISK ZONE
6-20 parcel:      4,738 clusters  (1.0%)   — moderate, usually already well-connected
20+ parcel:       2,029 clusters  (0.4%)   — low risk, fission can correct
```

**The user's intuition is correct but slightly reframed:**
Email deduplication doesn't "break" smaller clusters — it *merges* them. The risk is
**false-positive merges between unrelated small clusters** that happen to use the same
boutique RA firm. These merges are essentially permanent because Pass B fission only
triggers at ≥300 parcels.

**Example of risky scenario:**
- Entity A (2 parcels): residential investor, uses small RA firm X with email `firm@x.com`
- Entity B (2 parcels): unrelated restaurant owner, same RA firm
- ra_key() already creates an edge between them via shared RA name+street
- Email adds no new risk here — they're ALREADY connected

**Example where email adds genuine new risk:**
- Entity A: client of "Jane Smith, Attorney" (personal RA)
- Entity B: client of "Smith Legal LLC" (Jane's company)
- Same email, different ra_key → email creates a NEW edge not currently in the graph
- If A and B are genuinely unrelated business owners using the same small-firm attorney as
  registered agent, this is a false positive

**Key mitigating factor:** Any RA (firm or individual) serving many clients will hit the
MAX_RA_ENTITIES=500 threshold (same cap used for ra_key). So even if a boutique firm has
50 unrelated clients, the threshold prevents all 50 from getting spuriously merged.

### Compared to existing ra_key risk

The email approach is not fundamentally riskier than the current ra_key approach — both
bet that "shared RA = meaningful relationship." Email just widens the net to cross-name
variants of the same agent. The incremental false-positive risk is:

- Small if the email is truly personal/unique to one agent (Zephrina's `zcazaubon@...`)
- Medium if a small firm's info@ address is shared by multiple unaffiliated agents
- Large if the domain threshold is set too high (allowing commercial providers through)

The domain blocklist + per-email count threshold should catch the large-risk cases.

### What would NOT be improved by filing history

Filing history (`BizEntityFilingHistory.txt`, currently skipped) shows *when* agents
changed over time. It would not help RA identity deduplication — that requires
cross-referencing the agent records themselves, which the email field already provides.
Filing history would help a different problem: detecting RA changes that signal ownership
transfer (e.g., entity A had Azeez Khan as RA, then switched to ZMC — implies same
network even across time). That's a future enhancement, not related to this plan.

---

## Effect on Cluster 143 Specifically

Cluster 143 (the Azeez Khan / ZMC / Zephrina Cazaubon network) was the motivating example.
Email deduplication would cause it to **grow, not split**.

- The Zephrina Cazaubon personal RA entities (968 FERN LLC, AARIA HOLDINGS INC) and the
  ZMC & Associates LLC entities are **already in cluster 143** — connected via the shared
  1851 Peeler Rd mailing address, not via RA identity. Email would add redundant edges
  between things already in the same component.
- ZMC's ~10 known entities in the dataset all appear to already be in 143.
- Growth would only come from other ZMC/Zephrina clients **outside** cluster 143 that
  don't share the 1851 Peeler Rd address bridge. Given ZMC's small scale, this is likely
  zero or a handful of entities.

Cluster 143 is essentially already "complete" for the Zephrina/ZMC connection. The more
meaningful impact of email deduplication would appear in other, currently-separate clusters
where ZMC or Zephrina clients exist without the address bridge.

Note: clusters can only grow from new edges, never split. Splitting requires Pass B
fission (≥300 parcels). Cluster 143 has 57 parcels — immune to fission regardless.

---

## Verification (when this is eventually implemented)

1. Before rebuild: snapshot cluster count and cluster 143's composition
2. Run the modified pipeline on a sample county
3. Check cluster 143: expect minimal change (already well-connected via address)
4. Search for other clusters containing ZMC/Zephrina clients — confirm they merged
   into 143 or into each other as expected
5. Spot-check 5 small clusters (2-5 parcel) that use small RA firms — confirm they merged
   correctly or correctly stayed separate
6. Confirm no commercial RA emails slipped through (query: any new edges with
   rel='shared_ra_email' where email count in sos.registered_agents > 50)
7. Compare total cluster count before/after — expect modest decrease (merges) mostly
   among 2-10 parcel clusters
