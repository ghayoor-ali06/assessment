"""Geographic primitives: distance, polyline decoding, route resampling.

All distances are in statute miles. Coordinates are (latitude, longitude) in
degrees unless a name explicitly says radians.
"""

from __future__ import annotations

import math

import numpy as np

# Mean Earth radius in statute miles (IUGG mean radius 6371.0088 km).
EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344

# Bounding boxes used to keep requests inside the USA without a network call.
# (min_lat, max_lat, min_lon, max_lon)
US_BOUNDING_BOXES = (
    (24.396308, 49.384358, -125.000000, -66.934570),  # contiguous 48 + DC
    (51.214183, 71.538800, -179.148909, -129.974167),  # Alaska (mainland)
    (18.910361, 22.235400, -160.236053, -154.806773),  # Hawaii
)


def in_usa(lat: float, lon: float) -> bool:
    """Approximate US containment test, deliberately permissive.

    A rectangle cannot express the US border, so this accepts a margin of
    southern Canada and northern Mexico: Toronto and Tijuana both pass. That is
    the intended trade-off. The alternative, a coarse boundary polygon, was
    tried and rejected because at usable resolution it excludes real US
    territory such as Key West, and wrongly rejecting a legitimate US location
    is worse than accepting a point just over the border.

    Named locations get a stricter check: the geocoder is restricted to US
    results and Canadian regions are rejected outright (see
    ``routing.services.geocoding``). This guard exists to reject obviously
    foreign coordinates cheaply, before any external call is made.
    """
    return any(
        lo_lat <= lat <= hi_lat and lo_lon <= lon <= hi_lon
        for lo_lat, hi_lat, lo_lon, hi_lon in US_BOUNDING_BOXES
    )


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_lat = p2 - p1
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Decode a Google/OSRM encoded polyline into [(lat, lon), ...].

    OSRM's ``geometries=polyline6`` uses precision 6, which is ~5x more compact
    on the wire than the equivalent GeoJSON while carrying the same detail.
    """
    factor = float(10**precision)
    coordinates: list[tuple[float, float]] = []
    lat = lon = 0
    index = 0
    length = len(encoded)

    while index < length:
        for axis in range(2):
            shift = 0
            result = 0
            while True:
                if index >= length:
                    raise ValueError("truncated polyline")
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        coordinates.append((lat / factor, lon / factor))

    return coordinates


def cumulative_miles(points: np.ndarray, total_miles: float | None = None) -> np.ndarray:
    """Cumulative distance along a polyline, in miles.

    ``points`` is an (N, 2) array of (lat, lon) degrees. When ``total_miles`` is
    given, the result is rescaled so the final value matches it exactly. The
    polyline is a chain of great-circle hops, which slightly undershoots the
    true driving distance the router reports; rescaling keeps our mile markers
    consistent with the distance we show the user.
    """
    if len(points) < 2:
        return np.zeros(len(points), dtype=float)

    lat = np.radians(points[:, 0])
    lon = np.radians(points[:, 1])
    d_lat = np.diff(lat)
    d_lon = np.diff(lon)
    a = np.sin(d_lat / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(d_lon / 2) ** 2
    # Clip guards against tiny negatives from floating-point round-off.
    segments = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    cumulative = np.concatenate([[0.0], np.cumsum(segments)])

    if total_miles is not None and cumulative[-1] > 0:
        cumulative = cumulative * (total_miles / cumulative[-1])

    return cumulative


def resample_indices(cumulative: np.ndarray, step_miles: float = 2.0) -> np.ndarray:
    """Indices of polyline points spaced roughly ``step_miles`` apart.

    A cross-country route decodes to ~35k points. Testing every station against
    every point is wasteful when stations are matched to a corridor several
    miles wide, so we thin the route first. The first and last points are always
    retained so the origin and destination stay anchored.
    """
    if len(cumulative) == 0:
        return np.empty(0, dtype=int)
    if len(cumulative) <= 2 or cumulative[-1] <= 0:
        return np.arange(len(cumulative))

    targets = np.arange(0.0, cumulative[-1], max(step_miles, 0.01))
    indices = np.searchsorted(cumulative, targets)
    indices = np.append(indices, len(cumulative) - 1)
    return np.unique(np.clip(indices, 0, len(cumulative) - 1))
