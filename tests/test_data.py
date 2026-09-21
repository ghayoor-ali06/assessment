"""Integrity checks on the committed data files.

These guard the assumptions the rest of the code makes: that stations are in
the USA, priced sensibly, and deduplicated.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from django.conf import settings

from routing.services.gazetteer import CANADIAN_REGIONS, STATE_ABBREVIATIONS
from routing.services.geo import in_usa

STATIONS_PATH = Path(settings.DATA_DIR) / "stations.geocoded.csv"
PLACES_PATH = Path(settings.DATA_DIR) / "us_places.csv"

pytestmark = pytest.mark.skipif(
    not STATIONS_PATH.exists(),
    reason="run 'manage.py build_station_data' to generate the data files",
)


@pytest.fixture(scope="module")
def stations() -> list[dict]:
    with STATIONS_PATH.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_station_file_is_populated(stations):
    assert len(stations) > 6000


def test_every_station_is_in_a_us_state(stations):
    states = {row["state"] for row in stations}
    assert states <= STATE_ABBREVIATIONS


def test_no_canadian_rows_survived_the_import(stations):
    """The source file includes Canadian provinces; the brief is US-only."""
    states = {row["state"] for row in stations}
    assert not (states & (CANADIAN_REGIONS - STATE_ABBREVIATIONS))


def test_every_station_has_usable_coordinates(stations):
    for row in stations:
        lat, lon = float(row["lat"]), float(row["lon"])
        assert in_usa(lat, lon), f"{row['name']} ({row['city']}, {row['state']}) at {lat},{lon}"


def test_prices_are_positive_and_plausible(stations):
    prices = [float(row["price"]) for row in stations]
    assert all(price > 0 for price in prices)
    # The supplied snapshot spans roughly $2.69 to $6.40 per gallon.
    assert min(prices) > 1.0
    assert max(prices) < 15.0


def test_stations_are_deduplicated(stations):
    keys = [(row["opis_id"], row["name"]) for row in stations]
    assert len(keys) == len(set(keys))


def test_no_station_is_missing_a_name_or_city(stations):
    assert all(row["name"].strip() and row["city"].strip() for row in stations)


def test_coordinates_are_not_all_identical(stations):
    """A collapsed geocode would silently ruin every route."""
    assert len({(row["lat"], row["lon"]) for row in stations}) > 1000


def test_places_file_covers_every_state():
    with PLACES_PATH.open(newline="", encoding="utf-8") as handle:
        states = {row["state"] for row in csv.DictReader(handle)}
    missing = STATE_ABBREVIATIONS - states
    assert not missing, f"gazetteer is missing {sorted(missing)}"
