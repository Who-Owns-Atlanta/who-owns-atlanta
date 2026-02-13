Given what you’re trying to do (and the *Horizontal Holdings* methodology you’re following), you’re basically solving **large-scale, adversarial address normalization** rather than “just geocoding.” The good news is: a **fully free, very strong stack is absolutely possible**, but it needs to be layered and probabilistic rather than “one magic matcher.”

Below is how I’d think about it if I were building this for Atlanta-area parcel + ownership work.

---

## Core principle (important)

You **should not rely on a single matching step**. The paper’s Step 3 / Step 4 logic (“address matching” and “address derivatives”) works best when you:

1. Normalize addresses *as much as possible*
2. Generate **multiple comparable representations**
3. Match with **tiers of confidence**
4. Preserve ambiguity instead of forcing hard matches

PostGIS is ideal for orchestrating this.

---

## Recommended free stack (battle-tested)

### 1. **libpostal** (non-negotiable)

**Purpose:** aggressive, learned address parsing & normalization

* Repo: [https://github.com/openvenues/libpostal](https://github.com/openvenues/libpostal)
* Trained on OpenStreetMap + global address data
* Handles:

  * `Plaza on the Lake` vs `Plz On Lake`
  * `Ste 200` vs `#200` vs `Suite 200`
  * Weird punctuation, casing, abbreviations
* Outputs structured components *and* normalized strings

**Why it matters for your use case:**
This is the single best tool for collapsing “close but not exact” mailing addresses before you ever touch PostGIS similarity operators.

Typical pipeline:

```text
raw_owner_address
→ libpostal_parse
→ canonical components
→ normalized string
```

---

### 2. **PostGIS + pg_trgm**

**Purpose:** fuzzy matching & clustering at scale

Enable:

```sql
CREATE EXTENSION postgis;
CREATE EXTENSION pg_trgm;
CREATE EXTENSION unaccent;
```

What this gives you:

* `similarity(a, b)`
* `%` operator for trigram similarity
* Fast GIN indexes for fuzzy joins

Example:

```sql
SELECT *
FROM owners o1
JOIN owners o2
  ON similarity(o1.norm_addr, o2.norm_addr) > 0.85
 AND o1.id <> o2.id;
```

**This directly implements Step 3 / Step 4 from the paper**, but at scale.

---

### 3. **Local Nominatim (optional but powerful)**

**Purpose:** canonical spatial grounding

* Repo: [https://github.com/osm-search/Nominatim](https://github.com/osm-search/Nominatim)
* Uses OpenStreetMap data
* Fully self-hostable
* Free

Use it **only after libpostal**, and mainly to:

* Attach lat/lon to owner mailing addresses
* Normalize city/state/ZIP mismatches
* Detect “same building, different formatting”

You *do not* need rooftop precision — centroid-level is enough for clustering owner addresses.

---

### 4. **OSM-based street reference tables**

Instead of geocoding everything, you can also:

* Load OSM street centerlines into PostGIS
* Normalize street names against a canonical table

This helps with:

* `N Mopac Expy` vs `North MoPac Expressway`
* `I-285` vs `Interstate 285`

This is especially useful in Atlanta, where highway names are chaotic.

---

## Matching strategy (this is the real secret)

### Layer 1: **Exact canonical match**

After libpostal:

* Same normalized string → **100% confidence**

### Layer 2: **Component-aware fuzzy match**

Match on:

* Same house number
* Same ZIP or city
* High trigram similarity on street + unit

Example logic:

```sql
house_number = house_number
AND zip = zip
AND similarity(street_norm, street_norm) > 0.9
```

### Layer 3: **Spatial proximity**

If geocoded:

* Distance < 30 meters
* Same unit or no unit

This catches:

* Address formatting drift
* Suite vs floor vs building variations

### Layer 4: **Cluster, don’t match**

For adversarial cases (LLCs, law firms, registered agents):

* Use **connected components**
* One address links many entities
* Preserve graph edges instead of collapsing prematurely

This mirrors the paper’s *network logic* instead of a flat table.

---

## Ownership-network specific enhancements (from the paper)

These matter more than “perfect” address matching:

### A. **Address frequency heuristics**

Addresses that appear:

* 50+ times
* Across many LLCs
  → almost certainly management offices or registered agents

Flag them as **hubs**, not owners.

### B. **Name + address co-evolution**

Even when addresses drift:

* LLC name fragments remain stable
* Address fragments remain stable

Use *both*:

* `BAF ASSETS`
* `Plaza on the Lake`

Together they are extremely strong.

---

## Free tools you should *avoid* relying on alone

* USPS CASS (not free)
* Google / Mapbox APIs (rate limits + ToS issues)
* County “standardized” address fields (often worse than raw)

---

## A realistic “best possible” free architecture

```text
Raw assessor data
   ↓
libpostal (parse + normalize)
   ↓
PostGIS
  ├── exact matches
  ├── trigram similarity matches
  ├── spatial proximity matches
  └── graph clustering (ownership networks)
```

You will **never get 100% certainty**, but you *will* get:

* Extremely high recall
* Transparent uncertainty
* Reproducible methodology (key for research credibility)

---

## If you want, next steps

If you want, I can:

* Sketch **exact SQL schemas**
* Show **libpostal → Postgres ingestion code**
* Design a **confidence-scored address clustering table**
* Help tune thresholds specifically for **Metro Atlanta**

This is a very solvable problem with the right mindset — and you’re already thinking about it the right way.
