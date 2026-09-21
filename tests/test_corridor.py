"""Corridor matching against a synthetic route with known geometry."""

from __future__ import annotations

import numpy as np
import pytest

from routing.services.corridor import StationIndex, stations_along_route
from tests.conftest import make_station, straight_route

# A due-east line at 40 degrees north, from -100 to -90 longitude.
ROUTE = straight_route()
# Its true length, used so mile markers are scaled the way the API scales them.
ROUTE_MILES = 528.0


def match(records, corridor_miles=5.0, points=ROUTE, total=ROUTE_MILES):
    return stations_along_route(
        points, total, corridor_miles, index=StationIndex(records)
    )


def test_station_on_the_route_is_matched():
    found = match([make_station(1, 40.0, -95.0, 3.0)])
    assert len(found) == 1
    # Distance is measured to the nearest resampled route point rather than to
    # the line itself, so a station sitting exactly on the route reads as up to
    # half the 2-mile sampling step away. That slop is immaterial against a
    # 5-mile corridor and buys a large speed-up on cross-country routes.
    assert found[0].detour_miles == pytest.approx(0.0, abs=1.1)
    # Halfway along a 528-mile route.
    assert found[0].mile == pytest.approx(ROUTE_MILES / 2, abs=5.0)


def test_station_far_from_the_route_is_excluded():
    # Five degrees of latitude is roughly 345 miles off course.
    assert match([make_station(1, 45.0, -95.0, 3.0)]) == []


def test_corridor_width_is_respected():
    """A station ~20 miles north of the line is in or out depending on width."""
    station = make_station(1, 40.29, -95.0, 3.0)
    assert match([station], corridor_miles=5.0) == []
    assert len(match([station], corridor_miles=25.0)) == 1


def test_results_are_ordered_by_progress_along_route():
    records = [
        make_station(1, 40.0, -92.0, 3.0),
        make_station(2, 40.0, -98.0, 3.0),
        make_station(3, 40.0, -95.0, 3.0),
    ]
    found = match(records)
    miles = [item.mile for item in found]
    assert miles == sorted(miles)
    # Westernmost (-98) is nearest the origin at -100.
    assert [item.station.id for item in found] == [2, 3, 1]


def test_mile_markers_span_the_scaled_route_length():
    records = [
        make_station(1, 40.0, -99.9, 3.0),
        make_station(2, 40.0, -90.1, 3.0),
    ]
    found = match(records)
    assert found[0].mile == pytest.approx(0.0, abs=10.0)
    assert found[-1].mile == pytest.approx(ROUTE_MILES, abs=10.0)


def test_empty_station_table_returns_nothing():
    assert match([]) == []


def test_empty_route_returns_nothing():
    found = stations_along_route(
        np.empty((0, 2)), 0.0, 5.0, index=StationIndex([make_station(1, 40.0, -95.0, 3.0)])
    )
    assert found == []


def test_bounding_box_prefilter_does_not_drop_valid_stations():
    """A station just outside the raw route box but inside the corridor."""
    # Route ends at -90.0; this sits 3 miles further east, still within 5 miles.
    station = make_station(1, 40.0, -89.94, 3.0)
    assert len(match([station], corridor_miles=5.0)) == 1


def test_zero_length_route_still_matches_nearby_stations():
    """Start and finish at the same point must not crash the matcher."""
    points = np.array([[40.0, -100.0], [40.0, -100.0]])
    found = stations_along_route(
        points, 0.0, 5.0, index=StationIndex([make_station(1, 40.0, -100.0, 3.0)])
    )
    assert len(found) == 1
    assert found[0].mile == pytest.approx(0.0)


def test_large_station_set_is_handled_in_chunks():
    """Exercises the chunked distance computation beyond one block."""
    records = [
        make_station(i, 40.0 + (i % 3) * 0.01, -100.0 + i * 0.005, 3.0)
        for i in range(1500)
    ]
    found = match(records, corridor_miles=5.0)
    assert len(found) == 1500
    assert [item.mile for item in found] == sorted(item.mile for item in found)


def test_detour_distance_is_reported():
    # About 0.29 degrees of latitude, roughly 20 miles north of the route.
    found = match([make_station(1, 40.29, -95.0, 3.0)], corridor_miles=30.0)
    assert found[0].detour_miles == pytest.approx(20.0, abs=1.5)
