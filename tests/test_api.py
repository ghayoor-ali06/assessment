"""End-to-end API behaviour, with the routing service mocked.

Nothing here touches the network, so the suite is deterministic and fast. The
mock also counts calls, which is how the "one external call per request"
requirement is held in place.
"""

from __future__ import annotations

import numpy as np
import pytest
from django.urls import reverse

from routing.services import corridor as corridor_module
from routing.services import geocoding as geocoding_module
from routing.services import routing_provider
from routing.services.corridor import StationIndex
from routing.services.gazetteer import PlaceIndex
from routing.services.routing_provider import NoRouteFound, Route, RoutingError
from tests.conftest import make_station, straight_route

pytestmark = pytest.mark.django_db

ROUTE_MILES = 528.0


@pytest.fixture(autouse=True)
def places(monkeypatch):
    index = PlaceIndex()
    index.add("Westend", "NE", 40.0, -100.0)
    index.add("Eastend", "NE", 40.0, -90.0)
    monkeypatch.setattr(geocoding_module, "get_place_index", lambda: index)


@pytest.fixture(autouse=True)
def stations():
    """Four stations spread along the test route, cheapest in the middle."""
    corridor_module._index = StationIndex(
        [
            make_station(1, 40.0, -99.5, 3.50, name="Alpha", city="Westend"),
            make_station(2, 40.0, -97.0, 3.00, name="Bravo", city="Midtown"),
            make_station(3, 40.0, -94.0, 2.75, name="Charlie", city="Midtown"),
            make_station(4, 40.0, -91.0, 4.00, name="Delta", city="Eastend"),
        ]
    )
    yield
    corridor_module.reset_station_index()


@pytest.fixture
def routing_calls(monkeypatch):
    """Replace the provider with a counting stub."""
    calls = {"count": 0}

    def _fake(origin, destination):
        calls["count"] += 1
        return Route(
            distance_miles=ROUTE_MILES,
            duration_minutes=600.0,
            points=straight_route(),
            provider="osrm",
        )

    monkeypatch.setattr(routing_provider, "_fetch_osrm", _fake)
    monkeypatch.setitem(routing_provider._PROVIDERS, "osrm", _fake)
    return calls


def get(client, **params):
    params.setdefault("start", "Westend, NE")
    params.setdefault("finish", "Eastend, NE")
    return client.get(reverse("route"), params)


def test_happy_path(client, routing_calls):
    response = get(client)
    assert response.status_code == 200
    body = response.json()

    assert body["route"]["total_distance_miles"] == pytest.approx(ROUTE_MILES, abs=0.1)
    plan = body["fuel_plan"]
    assert plan["feasible"] is True
    # Every mile is paid for: 528 miles at 10 mpg.
    assert plan["total_gallons"] == pytest.approx(52.8, abs=0.05)
    assert plan["total_cost_usd"] > 0
    assert plan["stop_count"] >= 1
    assert body["meta"]["external_api_calls"] == 1
    assert routing_calls["count"] == 1


def test_exactly_one_external_call_per_cold_request(client, routing_calls):
    get(client)
    assert routing_calls["count"] == 1


def test_cache_hit_makes_no_external_call(client, routing_calls):
    first = get(client).json()
    second = get(client).json()

    assert first["meta"]["cached"] is False
    assert second["meta"]["cached"] is True
    assert second["meta"]["external_api_calls"] == 0
    assert routing_calls["count"] == 1  # still just the one
    assert second["fuel_plan"]["total_cost_usd"] == first["fuel_plan"]["total_cost_usd"]


def test_refresh_bypasses_the_cache(client, routing_calls):
    get(client)
    body = get(client, refresh="true").json()
    assert body["meta"]["cached"] is False
    assert routing_calls["count"] == 2


def test_cost_is_minimised_by_using_the_cheapest_reachable_station(client, routing_calls):
    """The plan must not simply buy at whichever station comes first."""
    body = get(client).json()
    prices = [stop["price_per_gallon"] for stop in body["fuel_plan"]["stops"]]
    # Charlie at $2.75 is the cheapest and must carry the largest purchase.
    biggest = max(body["fuel_plan"]["stops"], key=lambda s: s["gallons_purchased"])
    assert biggest["price_per_gallon"] == min(prices)


def test_stops_are_ordered_and_numbered(client, routing_calls):
    stops = get(client).json()["fuel_plan"]["stops"]
    assert [s["sequence"] for s in stops] == list(range(1, len(stops) + 1))
    miles = [s["distance_from_start_miles"] for s in stops]
    assert miles == sorted(miles)


def test_costs_sum_to_the_reported_total(client, routing_calls):
    plan = get(client).json()["fuel_plan"]
    assert sum(s["cost_usd"] for s in plan["stops"]) == pytest.approx(
        plan["total_cost_usd"], abs=0.02
    )
    assert sum(s["gallons_purchased"] for s in plan["stops"]) == pytest.approx(
        plan["total_gallons"], abs=0.02
    )


def test_geojson_contains_route_endpoints_and_stops(client, routing_calls):
    body = get(client).json()
    features = body["route"]["geojson"]["features"]
    kinds = [f["properties"]["kind"] for f in features]

    assert body["route"]["geojson"]["type"] == "FeatureCollection"
    assert kinds.count("route") == 1
    assert "start" in kinds and "finish" in kinds
    assert kinds.count("fuel_stop") == body["fuel_plan"]["stop_count"]

    line = next(f for f in features if f["properties"]["kind"] == "route")
    assert line["geometry"]["type"] == "LineString"
    # GeoJSON is [longitude, latitude].
    assert all(-180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in line["geometry"]["coordinates"])


def test_infeasible_route_returns_200_with_a_gap_diagnostic(client, routing_calls, monkeypatch):
    """Mirrors Seattle to Los Angeles: a real route with no usable stations."""
    corridor_module._index = StationIndex([make_station(1, 40.0, -99.9, 3.0)])

    body = get(client).json()
    plan = body["fuel_plan"]

    assert body["route"]["total_distance_miles"] > 0  # the map still comes back
    assert plan["feasible"] is False
    assert plan["reason"] == "no_station_in_range"
    assert plan["total_cost_usd"] is None
    assert plan["gap"]["gap_miles"] > 500


def test_no_stations_in_corridor(client, routing_calls):
    corridor_module._index = StationIndex([])
    plan = get(client).json()["fuel_plan"]
    assert plan["feasible"] is False
    assert plan["reason"] == "no_stations_in_corridor"


def test_same_start_and_finish(client, routing_calls, monkeypatch):
    def _zero(origin, destination):
        return Route(0.0, 0.0, np.array([[40.0, -100.0], [40.0, -100.0]]), "osrm")

    monkeypatch.setitem(routing_provider._PROVIDERS, "osrm", _zero)
    body = get(client, start="Westend, NE", finish="Westend, NE").json()

    assert body["fuel_plan"]["feasible"] is True
    assert body["fuel_plan"]["total_cost_usd"] == 0
    assert body["fuel_plan"]["stop_count"] == 0
    assert body["meta"]["warnings"]


def test_no_route_between_points_returns_422(client, monkeypatch):
    def _no_route(origin, destination):
        raise NoRouteFound("No drivable route connects these locations.")

    monkeypatch.setitem(routing_provider._PROVIDERS, "osrm", _no_route)
    response = get(client)
    assert response.status_code == 422
    assert response.json()["error"] == "no_route"


def test_routing_outage_returns_503(client, monkeypatch):
    def _down(origin, destination):
        raise RoutingError("connection refused")

    monkeypatch.setitem(routing_provider._PROVIDERS, "osrm", _down)
    monkeypatch.setitem(routing_provider._PROVIDERS, "valhalla", _down)
    response = get(client)
    assert response.status_code == 503
    assert response.json()["error"] == "routing_unavailable"


def test_falls_back_to_second_provider(client, monkeypatch):
    def _down(origin, destination):
        raise RoutingError("primary down")

    def _ok(origin, destination):
        return Route(ROUTE_MILES, 600.0, straight_route(), "valhalla")

    monkeypatch.setitem(routing_provider._PROVIDERS, "osrm", _down)
    monkeypatch.setitem(routing_provider._PROVIDERS, "valhalla", _ok)
    body = get(client).json()

    assert body["meta"]["routing_provider"] == "valhalla"
    assert body["meta"]["external_api_calls"] == 2  # the failure plus the retry


def test_unknown_location_returns_422_without_routing(client, routing_calls, settings):
    settings.ENABLE_REMOTE_GEOCODING = False
    response = get(client, start="Nowhere At All")
    assert response.status_code == 422
    assert response.json()["error"] == "location_not_found"
    # A bad location must not burn an external routing call.
    assert routing_calls["count"] == 0


def test_foreign_location_is_rejected_before_routing(client, routing_calls):
    response = get(client, start="48.8566,2.3522")
    assert response.status_code == 422
    assert routing_calls["count"] == 0


@pytest.mark.parametrize(
    "params",
    [
        {"start": ""},
        {"mpg": "0"},
        {"mpg": "-5"},
        {"range_miles": "0"},
        {"corridor_miles": "0"},
        {"mpg": "not-a-number"},
        {"cluster_bin_miles": "-1"},
    ],
)
def test_invalid_parameters_return_400(client, routing_calls, params):
    response = get(client, **params)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert routing_calls["count"] == 0


def test_missing_required_parameters_return_400(client):
    response = client.get(reverse("route"))
    assert response.status_code == 400
    errors = response.json()["detail"]
    assert "start" in errors and "finish" in errors


def test_custom_vehicle_parameters_change_the_answer(client, routing_calls):
    thirsty = get(client, mpg="5").json()["fuel_plan"]
    efficient = get(client, mpg="20").json()["fuel_plan"]
    assert thirsty["total_gallons"] == pytest.approx(ROUTE_MILES / 5, abs=0.1)
    assert efficient["total_gallons"] == pytest.approx(ROUTE_MILES / 20, abs=0.1)
    assert thirsty["total_cost_usd"] > efficient["total_cost_usd"]


def test_clustering_can_be_disabled(client, routing_calls):
    body = get(client, cluster_bin_miles="0").json()
    assert body["fuel_plan"]["feasible"] is True


def test_health_endpoint(client):
    body = client.get(reverse("health")).json()
    assert body["status"] == "ok"
    assert body["stations_loaded"] == 4


# --- geometry detail ---------------------------------------------------------
#
# The full road shape is ~97% of the payload. Callers that only want the fuel
# plan can ask for less, which also makes the response readable in a client.


def test_geometry_defaults_to_full(client, routing_calls):
    body = get(client).json()
    kinds = [f["properties"]["kind"] for f in body["route"]["geojson"]["features"]]
    assert "route" in kinds
    assert body["meta"]["geometry"] == "full"


def test_geometry_none_omits_the_road_line_but_keeps_the_stops(client, routing_calls):
    body = get(client, geometry="none").json()
    features = body["route"]["geojson"]["features"]
    kinds = [f["properties"]["kind"] for f in features]

    assert "route" not in kinds
    assert "start" in kinds and "finish" in kinds
    assert kinds.count("fuel_stop") == body["fuel_plan"]["stop_count"]
    # The plan itself is untouched.
    assert body["fuel_plan"]["total_cost_usd"] > 0
    assert body["route"]["total_distance_miles"] > 0


def test_geometry_simplified_is_smaller_than_full(client, routing_calls):
    def line_length(mode):
        body = get(client, geometry=mode).json()
        line = next(
            f
            for f in body["route"]["geojson"]["features"]
            if f["properties"]["kind"] == "route"
        )
        return len(line["geometry"]["coordinates"])

    full, simplified = line_length("full"), line_length("simplified")
    assert simplified < full
    assert simplified <= 100


def test_geometry_none_shrinks_the_payload(client, routing_calls):
    import json

    full = len(json.dumps(get(client).json()))
    without = len(json.dumps(get(client, geometry="none").json()))
    assert without < full / 2


def test_invalid_geometry_value_returns_400(client, routing_calls):
    response = get(client, geometry="tiny")
    assert response.status_code == 400
    assert "geometry" in response.json()["detail"]
