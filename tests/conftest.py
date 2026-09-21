"""Shared fixtures. No test in this suite touches the network."""

from __future__ import annotations

import numpy as np
import pytest
from django.core.cache import cache

from routing.services import corridor as corridor_module
from routing.services.corridor import StationIndex, StationRecord
from routing.services.routing_provider import Route


@pytest.fixture(autouse=True)
def clear_caches():
    """Keep route/geocode caching from leaking between tests."""
    cache.clear()
    corridor_module.reset_station_index()
    yield
    cache.clear()
    corridor_module.reset_station_index()


def make_station(station_id: int, lat: float, lon: float, price: float, **kwargs):
    return StationRecord(
        id=station_id,
        opis_id=kwargs.get("opis_id", str(station_id)),
        name=kwargs.get("name", f"Station {station_id}"),
        address=kwargs.get("address", "I-80"),
        city=kwargs.get("city", "Testville"),
        state=kwargs.get("state", "NE"),
        latitude=lat,
        longitude=lon,
        price=price,
    )


@pytest.fixture
def station_index_factory():
    """Install a synthetic station index for the duration of a test."""

    def _install(records: list[StationRecord]) -> StationIndex:
        index = StationIndex(records)
        corridor_module._index = index
        return index

    yield _install
    corridor_module.reset_station_index()


def straight_route(
    start_lat: float = 40.0,
    start_lon: float = -100.0,
    end_lon: float = -90.0,
    points: int = 400,
) -> np.ndarray:
    """A due-east line at constant latitude, easy to reason about."""
    lons = np.linspace(start_lon, end_lon, points)
    lats = np.full(points, start_lat)
    return np.column_stack([lats, lons])


@pytest.fixture
def fake_route():
    """Build a Route without calling a routing service."""

    def _build(distance_miles: float = 500.0, points: np.ndarray | None = None) -> Route:
        return Route(
            distance_miles=distance_miles,
            duration_minutes=distance_miles,
            points=straight_route() if points is None else points,
            provider="osrm",
        )

    return _build


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Also run smoke tests that call the real routing service.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: touches the network; only runs with --live"
    )


def pytest_collection_modifyitems(config, items):
    """Skip network-dependent tests unless --live is given."""
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs --live (calls the real routing service)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
