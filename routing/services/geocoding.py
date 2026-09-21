"""Resolve user-supplied locations to coordinates.

Ordered cheapest-first so the common case costs nothing:

1. ``"lat,lon"`` is parsed directly.
2. ``"City, ST"`` and ``"City, State Name"`` are looked up in the committed
   gazetteer, in process, with no network call.
3. Anything else (a full street address) falls through to Nominatim, cached.

Because the brief caps calls to the map/routing API, resolving locations
locally is what lets a typical request finish on a single external call.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from routing.services.gazetteer import (
    CANADIAN_REGIONS,
    STATE_ABBREVIATIONS,
    STATE_NAMES,
    get_place_index,
)
from routing.services.geo import in_usa


class GeocodingError(Exception):
    """The supplied location could not be resolved to a US coordinate."""


@dataclass(frozen=True)
class Location:
    query: str
    latitude: float
    longitude: float
    resolved_name: str
    source: str  # "coordinates", "gazetteer" or "nominatim"


_COORDINATE_PATTERN = re.compile(
    r"^\s*(?P<lat>[-+]?\d+(?:\.\d+)?)\s*[,/ ]\s*(?P<lon>[-+]?\d+(?:\.\d+)?)\s*$"
)


def _parse_coordinates(text: str) -> tuple[float, float] | None:
    match = _COORDINATE_PATTERN.match(text)
    if not match:
        return None
    latitude = float(match.group("lat"))
    longitude = float(match.group("lon"))
    # Reject NaN/inf and anything off the globe before it reaches a router.
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        raise GeocodingError(
            f"Coordinates out of range: latitude must be -90..90 and "
            f"longitude -180..180, got {latitude}, {longitude}."
        )
    return latitude, longitude


def _split_city_state(text: str) -> tuple[str, str] | None:
    """Pull ``("Kansas City", "MO")`` out of free text, if it looks like that."""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) < 2:
        # Also accept a trailing bare state, e.g. "Kansas City MO".
        words = text.strip().split()
        if len(words) >= 2 and words[-1].upper() in STATE_ABBREVIATIONS:
            return " ".join(words[:-1]), words[-1].upper()
        return None

    tail = parts[-1].upper().replace(".", "")
    # Tolerate a trailing country, e.g. "Denver, CO, USA".
    if tail in {"USA", "US", "UNITED STATES"} and len(parts) >= 3:
        parts = parts[:-1]
        tail = parts[-1].upper().replace(".", "")

    if tail in STATE_ABBREVIATIONS:
        state = tail
    elif tail in STATE_NAMES:
        state = STATE_NAMES[tail]
    else:
        return None
    return ", ".join(parts[:-1]).strip(), state


def _reject_canadian(text: str, label: str) -> None:
    """Fail fast on a Canadian region rather than matching a US namesake.

    Without this, "Toronto, Ontario" quietly resolves to a street in Ontario,
    California, because the geocoder is restricted to US results.
    """
    tail = text.split(",")[-1].strip().upper().replace(".", "")
    if tail in CANADIAN_REGIONS and tail not in STATE_ABBREVIATIONS:
        raise GeocodingError(
            f"The {label} appears to be in Canada. This API covers US routes only."
        )


def _geocode_remote(query: str) -> tuple[float, float, str]:
    """Last resort: one cached Nominatim lookup, restricted to the USA."""
    if not settings.ENABLE_REMOTE_GEOCODING:
        raise GeocodingError(
            f"Could not resolve {query!r}. Try 'City, ST' or 'latitude,longitude'."
        )

    key = "geocode:" + hashlib.sha256(query.lower().encode()).hexdigest()[:32]
    cached = cache.get(key)
    if cached is not None:
        if cached == "miss":
            raise GeocodingError(f"Could not find a US location matching {query!r}.")
        return cached["lat"], cached["lon"], cached["name"]

    try:
        response = requests.get(
            f"{settings.NOMINATIM_BASE_URL.rstrip('/')}/search",
            params={
                "q": query,
                "format": "json",
                "countrycodes": "us",
                "limit": 1,
            },
            headers={"User-Agent": settings.USER_AGENT},
            timeout=settings.ROUTING_TIMEOUT_SECONDS,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise GeocodingError(f"Geocoding service unavailable for {query!r}.") from exc

    if not payload:
        cache.set(key, "miss", settings.CACHE_TTL_SECONDS)
        raise GeocodingError(f"Could not find a US location matching {query!r}.")

    top = payload[0]
    latitude, longitude = float(top["lat"]), float(top["lon"])
    name = top.get("display_name", query)
    cache.set(
        key,
        {"lat": latitude, "lon": longitude, "name": name},
        settings.CACHE_TTL_SECONDS,
    )
    return latitude, longitude, name


def geocode(query: str, *, label: str = "location") -> Location:
    """Resolve ``query`` to a US coordinate, or raise :class:`GeocodingError`."""
    text = (query or "").strip()
    if not text:
        raise GeocodingError(f"The {label} is required.")

    coordinates = _parse_coordinates(text)
    if coordinates is not None:
        latitude, longitude = coordinates
        if not in_usa(latitude, longitude):
            raise GeocodingError(
                f"The {label} ({latitude:.4f}, {longitude:.4f}) is outside the USA. "
                "This API covers US routes only."
            )
        return Location(text, latitude, longitude, f"{latitude:.5f}, {longitude:.5f}", "coordinates")

    _reject_canadian(text, label)

    city_state = _split_city_state(text)
    if city_state is not None:
        city, state = city_state
        point = get_place_index().lookup(city, state)
        if point is not None:
            return Location(text, point[0], point[1], f"{city}, {state}", "gazetteer")

    latitude, longitude, name = _geocode_remote(text)
    if not in_usa(latitude, longitude):
        raise GeocodingError(
            f"The {label} resolved to {name!r}, which is outside the USA. "
            "This API covers US routes only."
        )
    return Location(text, latitude, longitude, name, "nominatim")
