"""Find the fuel stations that sit along a route.

Given a decoded route polyline, work out which stations are close enough to be
worth stopping at, and how far along the route each one is. That second number
is what the optimiser plans against.

The whole station table (~6,800 rows) is held in memory as numpy arrays. At
that size a vectorised sweep is faster than round-tripping to the database per
request, and it keeps the project free of a PostGIS dependency.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from routing.services.geo import EARTH_RADIUS_MILES, cumulative_miles, resample_indices

# Route points are compared against stations in blocks to bound peak memory:
# a 2,800-mile route resampled every 2 miles is ~1,400 points, which against
# 6,800 stations would otherwise allocate a 9.5M-element matrix at once.
CHUNK_SIZE = 512

MILES_PER_DEGREE_LATITUDE = 69.0


@dataclass(frozen=True)
class StationRecord:
    """Station fields needed to describe a stop in the API response."""

    id: int
    opis_id: str
    name: str
    address: str
    city: str
    state: str
    latitude: float
    longitude: float
    price: float


class StationIndex:
    """Station coordinates and prices in a layout suited to bulk maths."""

    def __init__(self, records: list[StationRecord]):
        self.records = records
        if records:
            self.latitudes = np.array([r.latitude for r in records], dtype=float)
            self.longitudes = np.array([r.longitude for r in records], dtype=float)
            self.prices = np.array([r.price for r in records], dtype=float)
        else:
            self.latitudes = np.empty(0)
            self.longitudes = np.empty(0)
            self.prices = np.empty(0)
        self.latitudes_rad = np.radians(self.latitudes)
        self.longitudes_rad = np.radians(self.longitudes)
        self.cos_latitudes = np.cos(self.latitudes_rad)

    def __len__(self) -> int:
        return len(self.records)


_index: StationIndex | None = None
_lock = threading.Lock()


def get_station_index() -> StationIndex:
    """Load the station table into memory once per process."""
    global _index
    if _index is not None:
        return _index
    with _lock:
        if _index is not None:
            return _index
        from routing.models import FuelStation

        records = [
            StationRecord(
                id=row[0],
                opis_id=row[1],
                name=row[2],
                address=row[3],
                city=row[4],
                state=row[5],
                latitude=row[6],
                longitude=row[7],
                price=float(row[8]),
            )
            for row in FuelStation.objects.values_list(
                "id",
                "opis_id",
                "name",
                "address",
                "city",
                "state",
                "latitude",
                "longitude",
                "price_per_gallon",
            ).iterator()
        ]
        _index = StationIndex(records)
        return _index


def reset_station_index() -> None:
    """Drop the cached index so the next request reloads it."""
    global _index
    with _lock:
        _index = None


@dataclass(frozen=True)
class NearbyStation:
    station: StationRecord
    mile: float  # distance from the origin along the route
    detour_miles: float  # perpendicular distance from the route


def stations_along_route(
    points: np.ndarray,
    total_miles: float,
    corridor_miles: float,
    *,
    step_miles: float = 2.0,
    index: StationIndex | None = None,
) -> list[NearbyStation]:
    """Stations within ``corridor_miles`` of the route, ordered by progress.

    ``points`` is an (N, 2) array of (lat, lon) degrees from the router.
    """
    index = index if index is not None else get_station_index()
    if len(index) == 0 or len(points) == 0:
        return []

    cumulative = cumulative_miles(points, total_miles=total_miles or None)
    keep = resample_indices(cumulative, step_miles=step_miles)
    route_lat = np.radians(points[keep, 0])
    route_lon = np.radians(points[keep, 1])
    route_mile = cumulative[keep]

    # Reject stations outside the route's bounding box before doing any
    # trigonometry. On a short route this discards almost the whole table and
    # is the difference between ~15 ms and a few hundred.
    lat_pad = corridor_miles / MILES_PER_DEGREE_LATITUDE
    mid_lat = float(np.mean(points[:, 0]))
    # A degree of longitude shrinks with latitude; guard the pole-ward limit.
    lon_scale = max(np.cos(np.radians(min(abs(mid_lat), 89.0))), 0.01)
    lon_pad = corridor_miles / (MILES_PER_DEGREE_LATITUDE * lon_scale)

    within_box = (
        (index.latitudes >= points[:, 0].min() - lat_pad)
        & (index.latitudes <= points[:, 0].max() + lat_pad)
        & (index.longitudes >= points[:, 1].min() - lon_pad)
        & (index.longitudes <= points[:, 1].max() + lon_pad)
    )
    candidate_ids = np.flatnonzero(within_box)
    if candidate_ids.size == 0:
        return []

    station_lat = index.latitudes_rad[candidate_ids][:, None]
    station_lon = index.longitudes_rad[candidate_ids][:, None]
    station_cos = index.cos_latitudes[candidate_ids][:, None]

    best_distance = np.full(candidate_ids.size, np.inf)
    best_mile = np.zeros(candidate_ids.size)

    for start in range(0, route_lat.size, CHUNK_SIZE):
        chunk_lat = route_lat[start : start + CHUNK_SIZE][None, :]
        chunk_lon = route_lon[start : start + CHUNK_SIZE][None, :]
        chunk_mile = route_mile[start : start + CHUNK_SIZE]

        d_lat = chunk_lat - station_lat
        d_lon = chunk_lon - station_lon
        a = np.sin(d_lat / 2) ** 2 + station_cos * np.cos(chunk_lat) * np.sin(d_lon / 2) ** 2
        distances = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))

        nearest = np.argmin(distances, axis=1)
        nearest_distance = distances[np.arange(candidate_ids.size), nearest]
        improved = nearest_distance < best_distance
        best_distance[improved] = nearest_distance[improved]
        best_mile[improved] = chunk_mile[nearest][improved]

    inside = np.flatnonzero(best_distance <= corridor_miles)
    nearby = [
        NearbyStation(
            station=index.records[candidate_ids[position]],
            mile=float(best_mile[position]),
            detour_miles=float(best_distance[position]),
        )
        for position in inside
    ]
    nearby.sort(key=lambda item: item.mile)
    return nearby
