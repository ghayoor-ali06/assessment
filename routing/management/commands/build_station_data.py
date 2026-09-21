"""Developer tool: turn the raw fuel-price CSV into geocoded station data.

This is **not** on the request path. It runs once, and its two outputs are
committed to the repository:

    data/us_places.csv          place name + state -> coordinates
    data/stations.geocoded.csv  cleaned, deduplicated, geocoded stations

Re-running it is only necessary if the price CSV changes.

The supplied CSV has no coordinates, so each station is placed by resolving its
city and state against the US Census gazetteer. Coverage is graded:

    Census "places"              ~93%   incorporated places and CDPs
    + county subdivisions        ~96%   New England towns are not "places"
    + name normalisation         ~96.4% accents, St./Ste., DeForest, hyphens
    + consolidated-gov prefixes  ~96.8% Nashville-Davidson, Macon-Bibb
    + Nominatim for the rest     100%   ~120 unincorporated communities

Nominatim answers are stored in data/geocode_cache.json and committed, so a
fresh checkout reproduces the data without touching the network.
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.services.gazetteer import (
    STATE_ABBREVIATIONS,
    PlaceIndex,
    normalize_place,
)

GAZETTEER_YEAR = 2026
GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "{year}_Gazetteer/{year}_Gaz_{dataset}_national.zip"
)
# Column offsets differ between the two files, so carry them explicitly.
GAZETTEER_FILES = (
    # dataset, name column, latitude column, longitude column
    ("place", 4, 11, 12),
    ("cousubs", 4, 10, 11),
)

NOMINATIM_DELAY_SECONDS = 1.2  # OSM policy allows at most 1 request/second.


class Command(BaseCommand):
    help = "Geocode the raw fuel-price CSV into committed station data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            default=None,
            help="Raw price CSV (default: data/fuel-prices-for-be-assessment.csv).",
        )
        parser.add_argument(
            "--cache-dir",
            default=None,
            help="Where to keep downloaded gazetteer archives (default: .gazetteer_cache).",
        )
        parser.add_argument(
            "--offline",
            action="store_true",
            help="Never call Nominatim; fail if the committed cache is insufficient.",
        )

    def handle(self, *args, **options):
        data_dir = Path(settings.DATA_DIR)
        source = Path(options["source"] or data_dir / "fuel-prices-for-be-assessment.csv")
        if not source.exists():
            raise CommandError(f"Price CSV not found: {source}")

        cache_dir = Path(options["cache_dir"] or Path(settings.BASE_DIR) / ".gazetteer_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        places = self._build_places(cache_dir, data_dir)
        rows = self._read_prices(source)
        self._write_stations(rows, places, data_dir, offline=options["offline"])

    # -- gazetteer ---------------------------------------------------------

    def _download(self, dataset: str, cache_dir: Path) -> bytes:
        archive = cache_dir / f"{GAZETTEER_YEAR}_Gaz_{dataset}.zip"
        if archive.exists():
            return archive.read_bytes()
        url = GAZETTEER_URL.format(year=GAZETTEER_YEAR, dataset=dataset)
        self.stdout.write(f"  downloading {dataset} gazetteer...")
        request = urllib.request.Request(url, headers={"User-Agent": settings.USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
        archive.write_bytes(payload)
        return payload

    def _build_places(self, cache_dir: Path, data_dir: Path) -> PlaceIndex:
        """Write data/us_places.csv and return the matching index.

        Returns a :class:`PlaceIndex` rather than a plain dict so build-time
        matching uses exactly the same fallbacks (squashed spellings,
        consolidated-government prefixes) as runtime lookups do.
        """
        self.stdout.write("Building US place gazetteer...")
        index = PlaceIndex()
        seen: set[tuple[str, str]] = set()
        records: list[tuple[str, str, float, float]] = []

        for dataset, name_col, lat_col, lon_col in GAZETTEER_FILES:
            payload = self._download(dataset, cache_dir)
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                raw = archive.read(archive.namelist()[0]).decode("utf-8", errors="replace")
            count = 0
            for line in raw.splitlines()[1:]:
                parts = line.split("|")
                if len(parts) <= max(name_col, lat_col, lon_col):
                    continue
                state = parts[0].strip().upper()
                if state not in STATE_ABBREVIATIONS:
                    continue
                try:
                    lat = float(parts[lat_col])
                    lon = float(parts[lon_col])
                except ValueError:
                    continue
                name = parts[name_col].strip()
                key = (normalize_place(name), state)
                if not key[0] or key in seen:
                    continue
                seen.add(key)
                index.add(name, state, lat, lon)
                records.append((name, state, lat, lon))
                count += 1
            self.stdout.write(f"  {dataset}: {count} rows")

        destination = data_dir / "us_places.csv"
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["name", "state", "lat", "lon"])
            writer.writerows(records)
        self.stdout.write(self.style.SUCCESS(f"  wrote {destination} ({len(records)} places)"))
        return index

    # -- prices ------------------------------------------------------------

    def _read_prices(self, source: Path) -> dict[tuple[str, str], dict]:
        """Clean, filter to the USA, and deduplicate the raw price rows."""
        self.stdout.write(f"Reading {source.name}...")
        stations: dict[tuple[str, str], dict] = {}
        total = skipped_foreign = skipped_bad = 0

        # utf-8-sig drops the byte-order mark some exports carry.
        with source.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                total += 1
                state = (row.get("State") or "").strip().upper()
                # The file includes Canadian provinces; the brief scopes this to
                # the USA, and their prices are not in USD per gallon anyway.
                if state not in STATE_ABBREVIATIONS:
                    skipped_foreign += 1
                    continue
                try:
                    price = float((row.get("Retail Price") or "").strip())
                except ValueError:
                    skipped_bad += 1
                    continue
                if price <= 0:
                    skipped_bad += 1
                    continue

                name = (row.get("Truckstop Name") or "").strip()
                city = (row.get("City") or "").strip()
                if not name or not city:
                    skipped_bad += 1
                    continue

                # The same physical stop appears several times under different
                # rack IDs and name spellings; key on the OPIS id plus name and
                # average the quoted prices.
                key = ((row.get("OPIS Truckstop ID") or "").strip(), name.upper())
                entry = stations.setdefault(
                    key,
                    {
                        "opis_id": key[0],
                        "name": name,
                        "address": (row.get("Address") or "").strip(),
                        "city": city,
                        "state": state,
                        "prices": [],
                    },
                )
                entry["prices"].append(price)

        self.stdout.write(
            f"  {total} rows -> {len(stations)} stations "
            f"({skipped_foreign} non-US, {skipped_bad} unusable)"
        )
        return stations

    # -- geocoding ---------------------------------------------------------

    def _write_stations(self, stations, places, data_dir: Path, *, offline: bool) -> None:
        cache_path = data_dir / "geocode_cache.json"
        cache: dict[str, list[float]] = {}
        if cache_path.exists():
            cache = json.loads(cache_path.read_text())

        self.stdout.write("Geocoding stations...")

        # Resolve each distinct city once, cheapest source first.
        wanted = {(entry["city"], entry["state"]) for entry in stations.values()}
        points: dict[tuple[str, str], tuple[float, float]] = {}
        pending: list[tuple[str, str]] = []
        from_gazetteer = from_cache = 0

        for city, state in sorted(wanted):
            point = places.lookup(city, state)
            if point is not None:
                from_gazetteer += 1
            else:
                cached = cache.get(f"{city.upper()}|{state}")
                if cached is not None:
                    point = (cached[0], cached[1])
                    from_cache += 1
                else:
                    pending.append((city, state))
                    continue
            points[(city, state)] = point

        if pending and not offline:
            self.stdout.write(
                f"  {len(pending)} places need Nominatim "
                f"(~{len(pending) * NOMINATIM_DELAY_SECONDS / 60:.1f} min at 1 req/s)"
            )
            for index, (city, state) in enumerate(pending, start=1):
                point = self._nominatim(city, state)
                if point is not None:
                    cache[f"{city.upper()}|{state}"] = list(point)
                    points[(city, state)] = point
                if index % 20 == 0:
                    self.stdout.write(f"    {index}/{len(pending)}")
            cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True))

        unresolved = sorted(f"{city}, {state}" for city, state in wanted - points.keys())
        if unresolved:
            self.stdout.write(
                self.style.WARNING(
                    f"  {len(unresolved)} places unresolved; their stations are "
                    f"omitted: {unresolved[:5]}"
                )
            )

        resolved = [
            {**entry, "lat": point[0], "lon": point[1]}
            for entry in stations.values()
            if (point := points.get((entry["city"], entry["state"]))) is not None
        ]

        destination = data_dir / "stations.geocoded.csv"
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["opis_id", "name", "address", "city", "state", "lat", "lon", "price"]
            )
            for entry in sorted(resolved, key=lambda e: (e["state"], e["city"], e["name"])):
                prices = entry["prices"]
                writer.writerow(
                    [
                        entry["opis_id"],
                        entry["name"],
                        entry["address"],
                        entry["city"],
                        entry["state"],
                        f"{entry['lat']:.6f}",
                        f"{entry['lon']:.6f}",
                        f"{sum(prices) / len(prices):.6f}",
                    ]
                )

        total = len(stations)
        self.stdout.write(
            self.style.SUCCESS(
                f"  wrote {destination}: {len(resolved)}/{total} stations "
                f"({100 * len(resolved) / total:.2f}% geocoded; "
                f"{from_gazetteer} gazetteer, {from_cache} cached)"
            )
        )

    def _nominatim(self, city: str, state: str) -> tuple[float, float] | None:
        """One rate-limited lookup, trying the most specific query first."""
        for query in (f"{city}, {state}, USA", f"{city}, {state}"):
            params = urllib.parse.urlencode(
                {"q": query, "format": "json", "countrycodes": "us", "limit": 1}
            )
            url = f"{settings.NOMINATIM_BASE_URL}/search?{params}"
            request = urllib.request.Request(url, headers={"User-Agent": settings.USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.load(response)
            except Exception as exc:  # noqa: BLE001 - best effort, keep going
                self.stderr.write(f"    nominatim failed for {city}, {state}: {exc}")
                payload = None
            time.sleep(NOMINATIM_DELAY_SECONDS)
            if payload:
                return float(payload[0]["lat"]), float(payload[0]["lon"])
        return None
