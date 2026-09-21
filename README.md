# Fuel Route Optimizer

A Django REST API that takes a start and finish inside the USA and returns the driving
route as GeoJSON, the **cost-optimal** places to buy fuel along it, and the total fuel
cost — assuming a 500-mile range and 10 miles per gallon.

Built on Django 6.1.1 (latest stable) and Django REST Framework 3.18.1.

```
GET /api/v1/route/?start=New York, NY&finish=Chicago, IL
```
```
795.8 miles · 79.58 gallons · 3 stops · $245.01 · 1 external API call · 1.3 s cold, 30 ms cached
```

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # needs Python 3.12+
pip install -r requirements.txt
python manage.py migrate
python manage.py import_stations                    # loads 6,854 geocoded stations
python manage.py runserver
```

Then:

```bash
curl "localhost:8000/api/v1/route/?start=New%20York,%20NY&finish=Chicago,%20IL"
```

No API keys, no database server, no PostGIS. The station data ships with the repo
already geocoded, so the import is a local CSV read.

Run the tests (all offline, no network):

```bash
pytest -q          # 328 tests, ~1 s
pytest -q --live   # also hits the real routing service (5 extra tests)
```

---

## The interesting problem: the price file has no coordinates

`fuel-prices-for-be-assessment.csv` gives each truck stop a name, address, city, state
and price — **but no latitude or longitude**. Without coordinates you cannot tell which
stations lie along a route, so this had to be solved before anything else.

Geocoding 3,808 distinct cities at request time is out of the question. Instead a
developer command, `build_station_data`, resolves them once, offline, and commits the
result. Coverage was built up in layers:

| Source | Cumulative coverage |
|---|---|
| US Census Gazetteer 2026 — *places* (31,806 rows) | 93.0% |
| **+** Gazetteer 2026 — *county subdivisions* (15,323 rows) | 95.9% |
| **+** name normalisation | 96.4% |
| **+** consolidated-government prefix matching | 96.8% |
| **+** one-off Nominatim pass for the remaining 120 | **100.00%** |

Three of those layers exist because of specific, real failures:

- **County subdivisions.** New England towns — Auburn NH, Berlin MA, Branford CT — are
  not Census "places" at all. Without this file, most of New England is unplaceable.
- **Normalisation.** Accents (Cañon City), abbreviations (St./Ste./Mt./Ft.), and
  spacing variants (`De Forest` vs `DeForest`, `Du Bois` vs `DuBois`).
- **Prefix matching.** Consolidated city-counties are listed under compound names:
  Nashville is `Nashville-Davidson metropolitan government (balance)`, Macon is
  `Macon-Bibb County`, Lexington is `Lexington-Fayette urban county government`.

The 120 stragglers are genuinely unincorporated communities — Breezewood PA, Clines
Corners NM, Doswell VA — that no gazetteer covers. Their coordinates live in
`data/geocode_cache.json`, committed, so a fresh clone reproduces the dataset with **no
network access at all**.

The same gazetteer then does double duty: it resolves the caller's `"City, ST"` input
in-process, which is why a typical request makes no geocoding call either.

### Other things the raw file needed

- **620 Canadian rows** (ON, AB, BC, MB, SK, YT, QC, NS, NB) are dropped. The brief
  scopes this to the USA, and those prices are not USD per gallon.
- **678 duplicate OPIS IDs.** The same physical stop is listed several times under
  different rack IDs and name spellings. These are collapsed to one row per stop with
  the price averaged: 8,151 raw rows become **6,854 stations**.

---

## Keeping external API calls to a minimum

The brief asks for one call to the map/routing API, and allows up to three.

**A cold request makes exactly one. A cached request makes zero.**

| Step | External calls |
|---|---|
| Resolve `"City, ST"` start and finish | **0** — local gazetteer |
| Fetch the route | **1** — OSRM |
| Find stations along the route | **0** — in-memory index |
| Choose stops and compute cost | **0** — local |

That one call returns distance, duration and the full route geometry together, so
nothing downstream needs to ask again. Only a full street address (rather than a city)
falls through to a geocoding service, and those responses are cached.

Every response reports its own call count, so this is verifiable rather than claimed:

```json
"meta": { "external_api_calls": 1, "cached": false, "elapsed_ms": 1263.4 }
```

**Routing provider:** the [OSRM](https://project-osrm.org/) public demo server — free,
keyless, and fast. `geometries=polyline6` is requested instead of GeoJSON because it is
about five times smaller on the wire (164 KB vs 804 KB on New York → Los Angeles) at the
same precision. `overview=full` is required: `simplified` collapses an 800-mile route to
roughly 27 points, far too coarse to place stations against. A
[Valhalla](https://valhalla1.openstreetmap.de/) instance is wired up as an automatic
fallback if OSRM is unreachable.

> The OSRM demo server is for reasonable, non-commercial use at up to 1 request/second
> and offers no uptime guarantee. For production, self-host OSRM and point
> `OSRM_BASE_URL` at it — no other change is needed.

---

## Performance

Measured end to end through HTTP, including the external call:

| Route | Distance | Cold | Cached |
|---|---|---|---|
| Boston → Providence | 49.5 mi | 0.96 s | **24 ms** |
| Dallas → Atlanta | 778.8 mi | 1.26 s | **51 ms** |
| New York → Chicago | 795.8 mi | 1.26 s | **30 ms** |
| Denver → Phoenix | 820.3 mi | 1.32 s | **45 ms** |
| New York → Los Angeles | 2,810.4 mi | 2.25 s | **283 ms** |
| Miami → Seattle | 3,304.3 mi | 2.18 s | **538 ms** |

Cold time is dominated by the network call to OSRM; our own work is 15–540 ms of it.
Three things keep that down:

1. **In-memory station index.** All 6,854 stations sit in numpy arrays, so corridor
   matching is a vectorised sweep rather than thousands of database round trips.
2. **Bounding-box prefilter.** Stations outside the route's box are discarded before any
   trigonometry — the difference between 15 ms and several hundred on a short route.
3. **Route caching.** Keyed on rounded coordinates, so a repeat request skips the
   external call entirely. Prices are a static snapshot, so the TTL is 24 h.
   The cache is Django's in-process `LocMemCache` — nothing to install and no
   service to run. It lives inside one process, so a multi-worker deployment
   would want a shared cache instead: set `REDIS_URL` (and `pip install redis`)
   and the same code path uses it.

---

## How the optimal stops are chosen

This is the classic **gas station problem** (Khuller, Malekian & Mestre, 2007). The
route is fixed, so the only decision is where to stop and how much to buy. At each
station, with price `p`:

- Find the first station within range that is **strictly cheaper** than `p`.
- If one exists → buy just enough fuel to reach it.
- If not → `p` is the best price in reach, so fill the tank, then drive to the
  **cheapest** station still reachable.
- Never buy more than is needed to finish the trip.

That last rule matters more than it looks. An earlier version drove to the *farthest*
reachable station instead of the cheapest; it passed every hand-written test but
overcharged by **$21 on New York → Los Angeles** ($877.54 against the true $856.26) and
produced 7 stops instead of 4 on Dallas → Atlanta. The bug is caught by
`test_greedy_matches_brute_force`, which checks the result against exhaustive dynamic
programming on 120 randomised instances.

### Fuel model — the one assumption worth stating

**The tank starts empty, so every mile driven is paid for.** Total gallons is always
`distance / mpg`, and the optimiser decides only how that spend is distributed.

The alternative — starting with a full tank — makes any trip under 500 miles cost $0
with no stops, which is a degenerate answer to "return the total money spent on fuel".

The first station is rarely at mile zero, so the opening miles are covered by a reserve
and settled up on arrival at that station's price. The stop that absorbs it says so:

```json
{ "sequence": 1, "distance_from_start_miles": 12.0, "gallons_purchased": 7.0,
  "covers_origin_miles": 12.0,
  "note": "Includes fuel for the first 12.0 mi from the origin, which the tank starts empty for and this stop pays for." }
```

### Station clustering

Truck stops bunch up around interchanges. Left alone, a strictly optimal plan will
prescribe a 0.38-gallon purchase at one station and another three miles later —
arithmetically correct, useless as advice. Only the cheapest station per 25-mile stretch
is considered by default. The measured cost difference on real routes is nil to a few
cents. Set `cluster_bin_miles=0` to disable it and see the raw optimum.

---

## API reference

### `GET /api/v1/route/`

| Parameter | Default | Notes |
|---|---|---|
| `start` | *required* | `"City, ST"`, a street address, or `"lat,lon"` |
| `finish` | *required* | same formats |
| `range_miles` | `500` | vehicle range on a full tank |
| `mpg` | `10` | fuel economy |
| `corridor_miles` | `5` | how far off-route a station may sit |
| `cluster_bin_miles` | `25` | keep cheapest station per N miles; `0` disables |
| `geometry` | `full` | road detail in the response: `full`, `simplified` or `none` |
| `refresh` | `false` | bypass the cache and re-fetch |

Accepted location formats: `Denver, CO` · `Denver, Colorado` · `Denver CO` ·
`Denver, CO, USA` · `39.7392,-104.9903`

<details>
<summary><b>Example response</b> (truncated)</summary>

```json
{
  "start":  { "query": "New York, NY", "resolved": "New York, NY",
              "latitude": 40.662712, "longitude": -73.938677, "source": "gazetteer" },
  "finish": { "query": "Chicago, IL", "resolved": "Chicago, IL",
              "latitude": 41.837045, "longitude": -87.684939, "source": "gazetteer" },
  "route": {
    "total_distance_miles": 795.8,
    "total_duration_minutes": 906.4,
    "geojson": { "type": "FeatureCollection", "features": [ "..." ] }
  },
  "fuel_plan": {
    "feasible": true,
    "total_gallons": 79.58,
    "total_cost_usd": 245.01,
    "average_price_per_gallon": 3.0789,
    "stop_count": 3,
    "stops": [
      { "sequence": 2, "distance_from_start_miles": 70.0,
        "name": "DELAWARE TRUCK STOP", "address": "I-80, EXIT 4C/US-46",
        "city": "Delaware", "state": "NJ", "opis_id": "67918",
        "latitude": 40.892819, "longitude": -75.069436, "detour_miles": 2.12,
        "price_per_gallon": 3.079, "gallons_purchased": 32.602, "cost_usd": 100.38 }
    ]
  },
  "meta": {
    "routing_provider": "osrm", "external_api_calls": 1, "cached": false,
    "elapsed_ms": 1263.4, "candidate_stations": 30, "warnings": []
  }
}
```
</details>

The `geojson` field is a ready-to-use `FeatureCollection` — the route `LineString`, the
start and finish, and a `Point` per fuel stop. Paste it into
[geojson.io](https://geojson.io) to see the map.

#### Trimming the road geometry

The full road shape is a few thousand coordinates and accounts for roughly 97% of the
payload, which makes the response awkward to read in a client. `geometry` controls it;
the plan and the totals are identical in every mode.

| `geometry` | Coordinates | Response | Pretty-printed |
|---|---|---|---|
| `full` (default) | 1,500 | 36.3 KB | 6,196 lines |
| `simplified` | 100 | 5.1 KB | 596 lines |
| `none` | 0 — stops only | 2.8 KB | 183 lines |

```bash
# just the fuel plan, readable on screen
curl "localhost:8000/api/v1/route/?start=New York, NY&finish=Chicago, IL&geometry=none"
```

### `GET /api/v1/health/`

Returns `{"status": "ok", "stations_loaded": 6854, "routing_provider": "osrm"}`.

### Status codes

| Code | When |
|---|---|
| `200` | Success — including a routable trip that cannot be fuelled (see below) |
| `400` | Malformed parameters (missing `start`, `mpg=0`, negative range) |
| `422` | Location cannot be resolved, is outside the USA, or no road route exists |
| `503` | Both routing providers unreachable |

---

## When a route cannot be fuelled

**The supplied price file has no stations along I-5 in California.** All 10 Californian
entries are in the far south-eastern desert — Brawley, Calexico, El Centro, Coachella.
There is nothing in Los Angeles, San Francisco, Sacramento or the Central Valley.

So Seattle → Los Angeles is a perfectly valid 1,135-mile drive with an **878-mile gap**
between the last usable station and the destination. At 500 miles of range, no plan
exists. Oregon (29 stations) and Rhode Island (2) are similarly thin.

This is a property of the data, not a bug, and the API says so precisely. It still
returns `200` with the full route and GeoJSON — the caller asked for a map and gets one
— alongside an exact diagnosis:

```json
"fuel_plan": {
  "feasible": false,
  "reason": "no_station_in_range",
  "detail": "878 mi gap between the last station (mile 258) and the destination (mile 1136); exceeds the 500 mi range.",
  "gap": { "from_mile": 258.3, "to_mile": 1135.9, "gap_miles": 877.6 },
  "total_cost_usd": null
}
```

Widening `corridor_miles` or raising `range_miles` will find a plan where one exists.

---

## Edge cases

Each of these is covered by a test.

| Case | Behaviour |
|---|---|
| Start and finish are the same place | `200`, zero distance, `$0.00`, no stops, explanatory warning |
| Route exists but cannot be fuelled | `200` with route + `feasible: false` + gap |
| Honolulu → Hilo, or a point in the ocean | `422` — OSRM `NoRoute`, handled not crashed |
| Coordinates outside the USA | `422` before any external call is made |
| A Canadian city (`Toronto, Ontario`) | `422` — otherwise it silently matches Ontario, **California** |
| Unresolvable place name | `422` naming which of start/finish failed |
| `mpg=0`, `range_miles=-5`, `lat=999` | `400` / `422` with a specific message |
| No stations within the corridor | `feasible: false`, reason `no_stations_in_corridor` |
| Gap of exactly 500 mi vs 500.1 mi | Feasible vs infeasible — the boundary is tested |
| Routing provider down | Falls back to Valhalla, then `503` |
| Anchorage → Seattle | Routes through Canada, where we hold no prices; reported as a gap |

### Known limitations

- **Station coordinates are city-level**, since that is all the price file provides. A
  stop is placed at its town, not at its exact forecourt, so `detour_miles` is
  indicative. The default 5-mile corridor accommodates this.
- **`in_usa` is a bounding-box test** and deliberately permissive: raw coordinates in
  southern Canada or northern Mexico are accepted. Tightening it enough to exclude
  Toronto also excludes Key West, and wrongly rejecting a real US location is the worse
  failure. Named locations get the stricter treatment — the geocoder is pinned to US
  results and Canadian regions are rejected outright.
- **Prices are a static snapshot** from the supplied file, not live data.

---

## Project layout

```
routing/
  models.py                      FuelStation
  services/
    geo.py                       distance, polyline decoding, resampling
    gazetteer.py                 place-name normalisation and lookup
    geocoding.py                 user input -> coordinates
    routing_provider.py          OSRM/Valhalla clients + route cache
    corridor.py                  in-memory spatial index
    optimizer.py                 the gas station problem
    planner.py                   orchestration
  api/                           serializers, views, urls
  management/commands/
    build_station_data.py        dev-only: builds the committed CSVs
    import_stations.py           loads them into the database
data/
  fuel-prices-for-be-assessment.csv   the supplied file
  stations.geocoded.csv               6,854 stations with coordinates
  us_places.csv                       47,129 US places
  geocode_cache.json                  the 120 Nominatim answers
tests/                           328 tests, fully offline
```

### Rebuilding the station data

Only needed if the price file changes:

```bash
python manage.py build_station_data          # re-geocodes, ~2 min for new places
python manage.py build_station_data --offline  # committed cache only, no network
python manage.py import_stations
```

---

## Configuration

Every setting has a working default, so the app runs with no configuration at
all. To change something, copy `.env.example` to `.env` (git-ignored) and edit
it; real environment variables override the file. The useful ones:

| Variable | Default | Purpose |
|---|---|---|
| `ROUTING_PROVIDER` | `osrm` | `osrm` or `valhalla` |
| `OSRM_BASE_URL` | public demo | point at a self-hosted instance for production |
| `USER_AGENT` | project string | OSM services require a real identifier |
| `CACHE_TTL_SECONDS` | `86400` | route cache lifetime |
| `REDIS_URL` | *(unset)* | optional shared cache for multi-worker deploys; needs `pip install redis` |
| `VEHICLE_RANGE_MILES` / `VEHICLE_MPG` | `500` / `10` | planning defaults |

## Attribution

Routing and geocoding by [OSRM](https://project-osrm.org/),
[Valhalla](https://valhalla1.openstreetmap.de/) and
[Nominatim](https://nominatim.openstreetmap.org/), all on
© [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (ODbL).
Place coordinates from the [US Census Bureau Gazetteer](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html) (public domain).
