"""Clients for the free routing services, and the cache in front of them.

The brief asks for as few calls to the map/routing API as possible. A request
that misses the cache makes exactly **one**: OSRM returns the distance,
duration and full geometry in a single response, and everything downstream
(corridor matching, fuel planning) is computed locally.

OSRM is the default because its public demo server is keyless and fast. A
Valhalla instance is wired up as a fallback for when the demo server is down.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache

from routing.services.geo import METERS_PER_MILE, decode_polyline

logger = logging.getLogger(__name__)


class RoutingError(Exception):
    """The routing service could not be reached or returned an error."""


class NoRouteFound(RoutingError):
    """The service answered, but no road route connects the two points.

    Happens for genuinely unreachable pairs: between Hawaiian islands, or when
    a coordinate falls in the ocean and cannot snap to the road network.
    """


@dataclass
class Route:
    distance_miles: float
    duration_minutes: float
    points: np.ndarray  # (N, 2) array of (lat, lon) degrees
    provider: str

    def to_line_coordinates(self, max_points: int = 1500) -> list[list[float]]:
        """GeoJSON [lon, lat] pairs, thinned for a sane response size.

        A cross-country route decodes to ~35k points; emitting all of them
        would dominate the payload without being visible on a map.
        """
        if len(self.points) <= max_points:
            selected = self.points
        else:
            keep = np.linspace(0, len(self.points) - 1, max_points).astype(int)
            selected = self.points[np.unique(keep)]
        return [[round(float(lon), 6), round(float(lat), 6)] for lat, lon in selected]


def _session() -> requests.Session:
    session = requests.Session()
    # OSM-hosted services require a genuine identifying User-Agent.
    session.headers.update({"User-Agent": settings.USER_AGENT})
    return session


def _fetch_osrm(origin, destination) -> Route:
    """One GET to OSRM; returns distance, duration and full geometry.

    ``polyline6`` is requested over GeoJSON because it is roughly five times
    smaller on the wire (164 KB vs 804 KB on a New York to Los Angeles route)
    at identical precision. ``overview=full`` is required: ``simplified``
    collapses an 800-mile route to about 27 points, far too coarse to place
    stations against.
    """
    url = (
        f"{settings.OSRM_BASE_URL.rstrip('/')}/route/v1/driving/"
        f"{origin[1]:.6f},{origin[0]:.6f};{destination[1]:.6f},{destination[0]:.6f}"
    )
    params = {
        "overview": "full",
        "geometries": "polyline6",
        "alternatives": "false",
        "steps": "false",
    }
    try:
        response = _session().get(
            url, params=params, timeout=settings.ROUTING_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise RoutingError(f"OSRM request failed: {exc}") from exc

    if response.status_code >= 500:
        raise RoutingError(f"OSRM returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RoutingError("OSRM returned a malformed response") from exc

    code = payload.get("code")
    if code in {"NoRoute", "NoSegment"}:
        raise NoRouteFound(
            "No drivable route connects these locations. They may be on "
            "separate road networks, such as different islands."
        )
    if code != "Ok" or not payload.get("routes"):
        raise RoutingError(f"OSRM error: {payload.get('message') or code or 'unknown'}")

    route = payload["routes"][0]
    geometry = route.get("geometry") or ""
    points = decode_polyline(geometry, precision=6) if geometry else []
    return Route(
        distance_miles=float(route["distance"]) / METERS_PER_MILE,
        duration_minutes=float(route["duration"]) / 60.0,
        points=np.array(points, dtype=float).reshape(-1, 2),
        provider="osrm",
    )


def _fetch_valhalla(origin, destination) -> Route:
    """Fallback provider, used only when OSRM is unreachable."""
    url = f"{settings.VALHALLA_BASE_URL.rstrip('/')}/route"
    payload = {
        "locations": [
            {"lat": origin[0], "lon": origin[1]},
            {"lat": destination[0], "lon": destination[1]},
        ],
        "costing": "auto",
        "directions_options": {"units": "miles"},
    }
    try:
        # Must be a POST with a JSON body. The older GET ?json=... form is
        # rejected by the public instance with "Failed to parse json request".
        response = _session().post(
            url, json=payload, timeout=settings.ROUTING_TIMEOUT_SECONDS
        )
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingError(f"Valhalla request failed: {exc}") from exc

    trip = body.get("trip")
    if not trip:
        # Distinguish "these points are not connected" from a request or
        # service fault, so the caller does not retry a definitive answer.
        code = body.get("error_code")
        if code in {442, 443}:  # no route / no segment
            raise NoRouteFound("No drivable route connects these locations.")
        raise RoutingError(f"Valhalla error: {body.get('error') or response.status_code}")
    if trip.get("status") != 0:
        raise NoRouteFound(
            trip.get("status_message") or "No drivable route connects these locations."
        )

    points: list[tuple[float, float]] = []
    for leg in trip.get("legs", []):
        # Valhalla encodes shapes at precision 6.
        points.extend(decode_polyline(leg.get("shape", ""), precision=6))

    summary = trip.get("summary", {})
    length = float(summary.get("length", 0.0))
    # We ask for miles, but honour whatever the service says it returned.
    if str(trip.get("units", "miles")).lower() in {"km", "kilometers", "kilometres"}:
        length *= 0.621371

    return Route(
        distance_miles=length,
        duration_minutes=float(summary.get("time", 0.0)) / 60.0,
        points=np.array(points, dtype=float).reshape(-1, 2),
        provider="valhalla",
    )


_PROVIDERS = {"osrm": _fetch_osrm, "valhalla": _fetch_valhalla}


def _cache_key(origin, destination) -> str:
    # Five decimal places is about a metre: precise enough that distinct
    # requests never collide, coarse enough that repeat queries share an entry.
    raw = (
        f"{settings.ROUTING_PROVIDER}:"
        f"{origin[0]:.5f},{origin[1]:.5f};{destination[0]:.5f},{destination[1]:.5f}"
    )
    return "route:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_route(origin, destination, *, use_cache: bool = True) -> tuple[Route, bool, int]:
    """Fetch a route, preferring the cache.

    Returns ``(route, was_cached, external_calls_made)``. A cache hit makes no
    external call at all, which is what keeps repeat requests in single-digit
    milliseconds.

    ``use_cache=False`` skips reading the cache but still refreshes it, so a
    forced refresh leaves a warm entry behind rather than an empty one.
    """
    key = _cache_key(origin, destination)
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return (
                Route(
                    distance_miles=cached["distance_miles"],
                    duration_minutes=cached["duration_minutes"],
                    points=np.array(cached["points"], dtype=float).reshape(-1, 2),
                    provider=cached["provider"],
                ),
                True,
                0,
            )

    primary = settings.ROUTING_PROVIDER
    fetch = _PROVIDERS.get(primary, _fetch_osrm)
    calls = 1
    try:
        route = fetch(origin, destination)
    except NoRouteFound:
        # A definitive "these points are not connected" answer. Retrying
        # elsewhere would waste a call for the same result.
        raise
    except RoutingError as exc:
        fallback = "valhalla" if primary == "osrm" else "osrm"
        logger.warning("%s routing failed (%s); trying %s", primary, exc, fallback)
        calls += 1
        route = _PROVIDERS[fallback](origin, destination)

    cache.set(
        key,
        {
            "distance_miles": route.distance_miles,
            "duration_minutes": route.duration_minutes,
            "points": route.points.tolist(),
            "provider": route.provider,
        },
        settings.CACHE_TTL_SECONDS,
    )
    return route, False, calls
