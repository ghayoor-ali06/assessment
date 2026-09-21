"""The route endpoint.

Returns the driving route as GeoJSON, the cost-optimal fuel stops along it, and
the total spend. A request that misses the cache makes exactly one call to the
external routing service.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from routing.api.serializers import RouteQuerySerializer
from routing.services.corridor import get_station_index
from routing.services.geocoding import GeocodingError
from routing.services.planner import PlanResult, plan_trip
from routing.services.routing_provider import NoRouteFound, RoutingError

logger = logging.getLogger(__name__)


@api_view(["GET"])
def route_view(request):
    query = RouteQuerySerializer(data=request.query_params)
    if not query.is_valid():
        return Response(
            {"error": "invalid_request", "detail": query.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )
    params = query.validated_data

    try:
        result = plan_trip(
            params["start"],
            params["finish"],
            range_miles=params["range_miles"],
            mpg=params["mpg"],
            corridor_miles=params["corridor_miles"],
            cluster_bin_miles=params["cluster_bin_miles"],
            use_cache=not params["refresh"],
        )
    except GeocodingError as exc:
        # The caller gave us something we cannot place: their input to fix.
        return Response(
            {"error": "location_not_found", "detail": str(exc)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except NoRouteFound as exc:
        return Response(
            {"error": "no_route", "detail": str(exc)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except RoutingError as exc:
        logger.exception("Routing provider unavailable")
        return Response(
            {"error": "routing_unavailable", "detail": str(exc)},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(_serialize(result))


@api_view(["GET"])
def health_view(request):
    """Liveness plus a count of loaded stations, handy during the demo."""
    return Response(
        {
            "status": "ok",
            "stations_loaded": len(get_station_index()),
            "routing_provider": settings.ROUTING_PROVIDER,
        }
    )


def _serialize(result: PlanResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "start": _location(result.start),
        "finish": _location(result.finish),
        "route": {
            "total_distance_miles": round(result.route.distance_miles, 1),
            "total_duration_minutes": round(result.route.duration_minutes, 1),
            "geojson": _geojson(result),
        },
        "fuel_plan": _fuel_plan(result),
        "meta": {
            "routing_provider": result.route.provider,
            "external_api_calls": result.external_calls,
            "cached": result.cached,
            "elapsed_ms": round(result.elapsed_ms, 1),
            "candidate_stations": result.candidates_considered,
            "warnings": result.warnings,
        },
    }
    return payload


def _location(location) -> dict[str, Any]:
    return {
        "query": location.query,
        "resolved": location.resolved_name,
        "latitude": round(location.latitude, 6),
        "longitude": round(location.longitude, 6),
        "source": location.source,
    }


def _fuel_plan(result: PlanResult) -> dict[str, Any]:
    if result.infeasible is not None:
        return {
            "feasible": False,
            "reason": result.infeasible.reason,
            "detail": result.infeasible.detail,
            "gap": result.infeasible.gap,
            "total_gallons": None,
            "total_cost_usd": None,
            "stop_count": 0,
            "stops": [],
        }

    plan = result.plan
    return {
        "feasible": True,
        "total_gallons": round(plan.total_gallons, 2),
        "total_cost_usd": round(plan.total_cost, 2),
        "average_price_per_gallon": round(plan.average_price, 4),
        "stop_count": len(result.stops),
        "stops": result.stops,
    }


def _geojson(result: PlanResult) -> dict[str, Any]:
    """Route line plus a marker per fuel stop, ready for geojson.io."""
    features: list[dict[str, Any]] = [
        {
            "type": "Feature",
            "properties": {
                "kind": "route",
                "distance_miles": round(result.route.distance_miles, 1),
                "provider": result.route.provider,
            },
            "geometry": {
                "type": "LineString",
                "coordinates": result.route.to_line_coordinates(),
            },
        }
    ]

    for endpoint, location in (("start", result.start), ("finish", result.finish)):
        features.append(
            {
                "type": "Feature",
                "properties": {"kind": endpoint, "name": location.resolved_name},
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        round(location.longitude, 6),
                        round(location.latitude, 6),
                    ],
                },
            }
        )

    for stop in result.stops:
        # A virtual origin fill-up has no coordinates of its own.
        if "longitude" not in stop:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "kind": "fuel_stop",
                    "sequence": stop["sequence"],
                    "name": stop["name"],
                    "city": stop["city"],
                    "state": stop["state"],
                    "price_per_gallon": stop["price_per_gallon"],
                    "gallons_purchased": stop["gallons_purchased"],
                    "cost_usd": stop["cost_usd"],
                    "distance_from_start_miles": stop["distance_from_start_miles"],
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [stop["longitude"], stop["latitude"]],
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}
