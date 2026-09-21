"""Opt-in smoke tests that call the real routing service and the real data.

Excluded from the default run because they need network access and depend on a
third-party demo server. Enable with:

    pytest --live

They exist to catch the failure the mocked suite structurally cannot: the
upstream API changing shape.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

pytestmark = [pytest.mark.live, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def require_stations(django_db_setup, django_db_blocker):
    """Load the committed station data into the test database."""
    from django.core.management import call_command

    from routing.models import FuelStation
    from routing.services.corridor import reset_station_index

    with django_db_blocker.unblock():
        if not FuelStation.objects.exists():
            call_command("import_stations", verbosity=0)
    reset_station_index()


def test_new_york_to_chicago(client):
    response = client.get(
        reverse("route"), {"start": "New York, NY", "finish": "Chicago, IL"}
    )
    assert response.status_code == 200
    body = response.json()

    assert 700 < body["route"]["total_distance_miles"] < 900
    plan = body["fuel_plan"]
    assert plan["feasible"] is True
    assert plan["total_gallons"] == pytest.approx(
        body["route"]["total_distance_miles"] / 10, rel=0.01
    )
    assert plan["stop_count"] >= 1
    assert body["meta"]["external_api_calls"] == 1


def test_seattle_to_los_angeles_is_reported_infeasible(client):
    """The price file has no stations along I-5 in California.

    The route itself is valid, so the API must still return it, with an
    explicit gap rather than a crash or a silently wrong plan.
    """
    response = client.get(
        reverse("route"), {"start": "Seattle, WA", "finish": "Los Angeles, CA"}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["route"]["total_distance_miles"] > 1000
    plan = body["fuel_plan"]
    assert plan["feasible"] is False
    assert plan["gap"]["gap_miles"] > 500


def test_repeat_request_is_served_from_cache(client):
    params = {"start": "Dallas, TX", "finish": "Atlanta, GA"}
    first = client.get(reverse("route"), params).json()
    second = client.get(reverse("route"), params).json()

    assert first["meta"]["cached"] is False
    assert second["meta"]["cached"] is True
    assert second["meta"]["external_api_calls"] == 0
    assert second["meta"]["elapsed_ms"] < first["meta"]["elapsed_ms"]


def test_valhalla_fallback_works_against_the_real_service():
    """The fallback is only useful if it actually works when OSRM is down.

    A mocked test cannot catch the public instance changing its request
    format, which is exactly what happened once already.
    """
    from routing.services.routing_provider import _fetch_valhalla

    route = _fetch_valhalla((40.7128, -74.0060), (41.8781, -87.6298))

    assert route.provider == "valhalla"
    assert 700 < route.distance_miles < 900
    assert len(route.points) > 100
    # Geometry must decode at the right precision, or it lands in the ocean.
    assert route.points[0] == pytest.approx([40.7128, -74.0060], abs=0.05)
    assert route.points[-1] == pytest.approx([41.8781, -87.6298], abs=0.05)


def test_failover_from_a_broken_primary_to_valhalla(settings, client):
    """End-to-end: point OSRM at a dead host and confirm a real route still
    comes back, from the other provider."""
    settings.OSRM_BASE_URL = "https://127.0.0.1:9"  # nothing listening
    settings.ROUTING_TIMEOUT_SECONDS = 5

    response = client.get(
        reverse("route"), {"start": "New York, NY", "finish": "Chicago, IL"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["routing_provider"] == "valhalla"
    assert body["fuel_plan"]["feasible"] is True
    assert body["fuel_plan"]["total_cost_usd"] > 0
