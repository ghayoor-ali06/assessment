"""Orchestrates a full request: locate, route, match stations, plan fuel.

Keeping this in one place means the view stays thin and the whole pipeline can
be exercised in tests without going through HTTP.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from routing.services.corridor import stations_along_route
from routing.services.geocoding import Location, geocode
from routing.services.optimizer import (
    Candidate,
    FuelPlan,
    InfeasibleRoute,
    cluster_candidates,
    plan_fuel_stops,
)
from routing.services.routing_provider import Route, get_route


@dataclass
class PlanResult:
    start: Location
    finish: Location
    route: Route
    plan: FuelPlan | None
    stops: list[dict[str, Any]] = field(default_factory=list)
    infeasible: InfeasibleRoute | None = None
    candidates_considered: int = 0
    cached: bool = False
    external_calls: int = 0
    elapsed_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)


def plan_trip(
    start_query: str,
    finish_query: str,
    *,
    range_miles: float,
    mpg: float,
    corridor_miles: float,
    cluster_bin_miles: float,
    use_cache: bool = True,
) -> PlanResult:
    started = time.perf_counter()

    # Geocoding happens before routing so a bad location fails fast, without
    # spending an external routing call.
    start = geocode(start_query, label="start")
    finish = geocode(finish_query, label="finish")

    route, cached, external_calls = get_route(
        (start.latitude, start.longitude),
        (finish.latitude, finish.longitude),
        use_cache=use_cache,
    )

    result = PlanResult(
        start=start,
        finish=finish,
        route=route,
        plan=None,
        cached=cached,
        external_calls=external_calls,
    )

    # Same start and finish: a legitimate request with nothing to optimise.
    if route.distance_miles <= 0 or len(route.points) < 2:
        result.plan = FuelPlan()
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        if route.distance_miles <= 0:
            result.warnings.append(
                "Start and finish resolve to the same place; no fuel is needed."
            )
        return result

    nearby = stations_along_route(route.points, route.distance_miles, corridor_miles)
    candidates = [
        Candidate(mile=item.mile, price=item.station.price, station=item)
        for item in nearby
    ]
    candidates = cluster_candidates(candidates, cluster_bin_miles)
    result.candidates_considered = len(candidates)

    try:
        plan = plan_fuel_stops(
            candidates, route.distance_miles, range_miles=range_miles, mpg=mpg
        )
    except InfeasibleRoute as exc:
        # Still a successful request: the caller asked for a route and gets
        # one, plus a precise explanation of why it cannot be fuelled.
        result.infeasible = exc
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result

    result.plan = plan
    result.stops = [_describe_stop(index, stop) for index, stop in enumerate(plan.stops, 1)]
    result.elapsed_ms = (time.perf_counter() - started) * 1000
    return result


def _describe_stop(sequence: int, stop) -> dict[str, Any]:
    nearby = stop.station  # NearbyStation
    station = nearby.station if nearby is not None else None
    described: dict[str, Any] = {
        "sequence": sequence,
        "distance_from_start_miles": round(stop.mile, 1),
        "gallons_purchased": round(stop.gallons, 3),
        "price_per_gallon": round(stop.price, 4),
        "cost_usd": round(stop.cost, 2),
    }
    if station is not None:
        described.update(
            {
                "station_id": station.id,
                "opis_id": station.opis_id,
                "name": station.name,
                "address": station.address,
                "city": station.city,
                "state": station.state,
                "latitude": round(station.latitude, 6),
                "longitude": round(station.longitude, 6),
                "detour_miles": round(nearby.detour_miles, 2),
            }
        )
    if stop.covers_origin_miles > 0:
        described["covers_origin_miles"] = round(stop.covers_origin_miles, 1)
        described["note"] = (
            f"Includes fuel for the first {stop.covers_origin_miles:.1f} mi from the "
            "origin, which the tank starts empty for and this stop pays for."
        )
    return described
