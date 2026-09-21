"""Request validation for the route endpoint."""

from __future__ import annotations

from django.conf import settings
from rest_framework import serializers


class RouteQuerySerializer(serializers.Serializer):
    """Validates and defaults the query string.

    The vehicle parameters are exposed so the assessment's fixed assumptions
    (500 mi range, 10 mpg) can be varied without a code change; the defaults
    match the brief.
    """

    start = serializers.CharField(
        max_length=255,
        help_text="Origin: 'City, ST', a street address, or 'latitude,longitude'.",
    )
    finish = serializers.CharField(
        max_length=255,
        help_text="Destination, in the same formats as start.",
    )
    range_miles = serializers.FloatField(
        required=False,
        min_value=1.0,
        max_value=10000.0,
        default=lambda: settings.VEHICLE_RANGE_MILES,
    )
    mpg = serializers.FloatField(
        required=False,
        min_value=0.1,
        max_value=500.0,
        default=lambda: settings.VEHICLE_MPG,
    )
    corridor_miles = serializers.FloatField(
        required=False,
        min_value=0.1,
        max_value=100.0,
        default=lambda: settings.CORRIDOR_MILES,
        help_text="How far off the route a station may sit.",
    )
    cluster_bin_miles = serializers.FloatField(
        required=False,
        min_value=0.0,
        max_value=250.0,
        default=lambda: settings.CLUSTER_BIN_MILES,
        help_text="Keep only the cheapest station per this many miles; 0 disables.",
    )
    refresh = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Bypass the cache and re-fetch the route.",
    )
    geometry = serializers.ChoiceField(
        required=False,
        choices=["full", "simplified", "none"],
        default="full",
        help_text=(
            "How much road geometry to return. 'full' (default) is map-ready but "
            "dominates the payload; 'simplified' is a lighter outline; 'none' "
            "omits the road line and returns just the stops."
        ),
    )

    def validate_start(self, value: str) -> str:
        return self._require_text(value, "start")

    def validate_finish(self, value: str) -> str:
        return self._require_text(value, "finish")

    @staticmethod
    def _require_text(value: str, field: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError(f"The {field} location cannot be blank.")
        return cleaned
