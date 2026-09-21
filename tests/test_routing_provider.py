"""Routing client: response parsing, error mapping and caching."""

from __future__ import annotations

import pytest

from routing.services import routing_provider
from routing.services.routing_provider import (
    NoRouteFound,
    Route,
    RoutingError,
    _fetch_osrm,
    get_route,
)
from tests.conftest import straight_route

DENVER = (39.7392, -104.9903)
CHICAGO = (41.8781, -87.6298)


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def osrm_payload():
    """A minimal but realistic OSRM reply, geometry encoded at precision 6."""
    return {
        "code": "Ok",
        "routes": [
            {
                # 1,609,344 m is exactly 1,000 miles.
                "distance": 1609344.0,
                "duration": 54000.0,
                "geometry": "_c`|@_gjaR_ibE_ibE",
            }
        ],
    }


def patch_get(monkeypatch, response):
    class _Session:
        headers: dict = {}

        def get(self, *args, **kwargs):
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(routing_provider, "_session", lambda: _Session())


def test_parses_distance_duration_and_geometry(monkeypatch, osrm_payload):
    patch_get(monkeypatch, FakeResponse(osrm_payload))
    route = _fetch_osrm(DENVER, CHICAGO)

    assert route.distance_miles == pytest.approx(1000.0)
    assert route.duration_minutes == pytest.approx(900.0)
    assert route.provider == "osrm"
    assert route.points.shape[1] == 2
    assert len(route.points) == 2


def test_requests_a_full_compact_geometry(monkeypatch, osrm_payload):
    """overview=full is required; simplified is far too coarse to match against."""
    seen = {}

    class _Session:
        headers: dict = {}

        def get(self, url, params=None, timeout=None):
            seen["url"] = url
            seen["params"] = params
            return FakeResponse(osrm_payload)

    monkeypatch.setattr(routing_provider, "_session", lambda: _Session())
    _fetch_osrm(DENVER, CHICAGO)

    assert seen["params"]["overview"] == "full"
    assert seen["params"]["geometries"] == "polyline6"
    assert seen["params"]["alternatives"] == "false"
    # OSRM takes longitude first.
    assert "-104.990300,39.739200;-87.629800,41.878100" in seen["url"]


@pytest.mark.parametrize("code", ["NoRoute", "NoSegment"])
def test_unreachable_pairs_raise_no_route(monkeypatch, code):
    patch_get(monkeypatch, FakeResponse({"code": code, "message": "Impossible route"}))
    with pytest.raises(NoRouteFound):
        _fetch_osrm(DENVER, CHICAGO)


def test_server_error_raises_routing_error(monkeypatch):
    patch_get(monkeypatch, FakeResponse({}, status_code=503))
    with pytest.raises(RoutingError):
        _fetch_osrm(DENVER, CHICAGO)


def test_malformed_body_raises_routing_error(monkeypatch):
    patch_get(monkeypatch, FakeResponse(None, text="<html>nope</html>"))
    with pytest.raises(RoutingError):
        _fetch_osrm(DENVER, CHICAGO)


def test_empty_route_list_raises_routing_error(monkeypatch):
    patch_get(monkeypatch, FakeResponse({"code": "Ok", "routes": []}))
    with pytest.raises(RoutingError):
        _fetch_osrm(DENVER, CHICAGO)


def test_cache_prevents_a_second_call(monkeypatch):
    calls = {"count": 0}

    def _fetch(origin, destination):
        calls["count"] += 1
        return Route(500.0, 600.0, straight_route(), "osrm")

    monkeypatch.setitem(routing_provider._PROVIDERS, "osrm", _fetch)

    first, cached_first, external_first = get_route(DENVER, CHICAGO)
    second, cached_second, external_second = get_route(DENVER, CHICAGO)

    assert (cached_first, external_first) == (False, 1)
    assert (cached_second, external_second) == (True, 0)
    assert calls["count"] == 1
    assert second.distance_miles == first.distance_miles
    assert len(second.points) == len(first.points)


def test_cache_distinguishes_different_journeys(monkeypatch):
    calls = {"count": 0}

    def _fetch(origin, destination):
        calls["count"] += 1
        return Route(500.0, 600.0, straight_route(), "osrm")

    monkeypatch.setitem(routing_provider._PROVIDERS, "osrm", _fetch)
    get_route(DENVER, CHICAGO)
    get_route(CHICAGO, DENVER)
    assert calls["count"] == 2


def test_no_route_is_not_retried_on_the_fallback(monkeypatch):
    """A definitive answer must not cost a second external call."""
    fallback_calls = {"count": 0}

    def _primary(origin, destination):
        raise NoRouteFound("islands")

    def _fallback(origin, destination):
        fallback_calls["count"] += 1
        return Route(1.0, 1.0, straight_route(), "valhalla")

    monkeypatch.setitem(routing_provider._PROVIDERS, "osrm", _primary)
    monkeypatch.setitem(routing_provider._PROVIDERS, "valhalla", _fallback)

    with pytest.raises(NoRouteFound):
        get_route(DENVER, CHICAGO)
    assert fallback_calls["count"] == 0


def test_line_coordinates_are_thinned_for_the_response():
    route = Route(2800.0, 2800.0, straight_route(points=40000), "osrm")
    coordinates = route.to_line_coordinates(max_points=1500)
    assert len(coordinates) <= 1500
    assert coordinates[0] == pytest.approx([-100.0, 40.0], abs=1e-4)


def test_short_route_geometry_is_not_thinned():
    route = Route(10.0, 10.0, straight_route(points=50), "osrm")
    assert len(route.to_line_coordinates(max_points=1500)) == 50


def test_refresh_still_warms_the_cache(monkeypatch):
    """A forced refresh must leave a usable entry, not an empty one."""
    calls = {"count": 0}

    def _fetch(origin, destination):
        calls["count"] += 1
        return Route(500.0, 600.0, straight_route(), "osrm")

    monkeypatch.setitem(routing_provider._PROVIDERS, "osrm", _fetch)

    get_route(DENVER, CHICAGO, use_cache=False)
    _, cached, external = get_route(DENVER, CHICAGO)

    assert cached is True
    assert external == 0
    assert calls["count"] == 1


# --- Valhalla fallback -------------------------------------------------------
#
# The public Valhalla instance rejects the older GET ?json=... form with
# "Failed to parse json request", so the client must POST a JSON body. These
# tests pin that down; test_live_smoke covers the real service.


class _PostSession:
    headers: dict = {}

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return FakeResponse(self.payload, status_code=self.status_code)


VALHALLA_OK = {
    "trip": {
        "status": 0,
        "units": "miles",
        "summary": {"length": 796.98, "time": 45269.9},
        "legs": [{"shape": "_c`|@_gjaR_ibE_ibE"}],
    }
}


def test_valhalla_posts_a_json_body(monkeypatch):
    session = _PostSession(VALHALLA_OK)
    monkeypatch.setattr(routing_provider, "_session", lambda: session)

    route = routing_provider._fetch_valhalla(DENVER, CHICAGO)

    assert len(session.calls) == 1
    body = session.calls[0]["json"]
    assert body["locations"] == [
        {"lat": 39.7392, "lon": -104.9903},
        {"lat": 41.8781, "lon": -87.6298},
    ]
    assert body["costing"] == "auto"
    assert body["directions_options"]["units"] == "miles"
    assert route.provider == "valhalla"
    assert route.distance_miles == pytest.approx(796.98)
    assert route.duration_minutes == pytest.approx(754.5, abs=0.1)


def test_valhalla_converts_kilometres(monkeypatch):
    payload = {
        "trip": {
            "status": 0,
            "units": "km",
            "summary": {"length": 100.0, "time": 3600.0},
            "legs": [{"shape": "_c`|@_gjaR"}],
        }
    }
    monkeypatch.setattr(routing_provider, "_session", lambda: _PostSession(payload))
    route = routing_provider._fetch_valhalla(DENVER, CHICAGO)
    assert route.distance_miles == pytest.approx(62.1371, abs=0.01)


@pytest.mark.parametrize("error_code", [442, 443])
def test_valhalla_unroutable_raises_no_route(monkeypatch, error_code):
    payload = {"error_code": error_code, "error": "No path could be found"}
    monkeypatch.setattr(
        routing_provider, "_session", lambda: _PostSession(payload, status_code=400)
    )
    with pytest.raises(NoRouteFound):
        routing_provider._fetch_valhalla(DENVER, CHICAGO)


def test_valhalla_request_fault_raises_routing_error(monkeypatch):
    """A malformed request is our bug, not an unroutable pair, so it must not
    masquerade as NoRouteFound and suppress the fallback."""
    payload = {"error_code": 100, "error": "Failed to parse json request"}
    monkeypatch.setattr(
        routing_provider, "_session", lambda: _PostSession(payload, status_code=400)
    )
    with pytest.raises(RoutingError):
        routing_provider._fetch_valhalla(DENVER, CHICAGO)
