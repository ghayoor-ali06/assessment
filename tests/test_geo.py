"""Geographic primitives."""

from __future__ import annotations

import numpy as np
import pytest

from routing.services.geo import (
    cumulative_miles,
    decode_polyline,
    haversine_miles,
    in_usa,
    resample_indices,
)


@pytest.mark.parametrize(
    "a,b,expected_miles",
    [
        # Published great-circle distances, allowing ~1% for the spherical model.
        ((40.7128, -74.0060), (41.8781, -87.6298), 711),  # New York - Chicago
        ((34.0522, -118.2437), (37.7749, -122.4194), 347),  # LA - San Francisco
        ((39.7392, -104.9903), (32.7767, -96.7970), 663),  # Denver - Dallas
    ],
)
def test_haversine_matches_known_distances(a, b, expected_miles):
    assert haversine_miles(*a, *b) == pytest.approx(expected_miles, rel=0.01)


def test_haversine_is_zero_for_identical_points():
    assert haversine_miles(40.0, -100.0, 40.0, -100.0) == pytest.approx(0.0)


def test_haversine_is_symmetric():
    forward = haversine_miles(40.0, -100.0, 45.0, -90.0)
    backward = haversine_miles(45.0, -90.0, 40.0, -100.0)
    assert forward == pytest.approx(backward)


def test_decode_polyline_round_trips_known_fixture():
    """Precision-5 example from Google's polyline algorithm documentation."""
    decoded = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert decoded == [
        pytest.approx((38.5, -120.2)),
        pytest.approx((40.7, -120.95)),
        pytest.approx((43.252, -126.453)),
    ]


def test_decode_polyline_precision_six():
    encoded = "_c`|@_gjaR"  # single point at precision 6
    decoded = decode_polyline(encoded, precision=6)
    assert len(decoded) == 1
    assert -90 <= decoded[0][0] <= 90
    assert -180 <= decoded[0][1] <= 180


def test_decode_empty_polyline():
    assert decode_polyline("", precision=6) == []


def test_decode_truncated_polyline_raises():
    with pytest.raises(ValueError):
        decode_polyline("_p~iF~ps|U_ulL", precision=5)


def test_cumulative_miles_is_monotonic_and_starts_at_zero():
    points = np.array([[40.0, -100.0], [40.0, -99.0], [40.0, -98.0]])
    cumulative = cumulative_miles(points)
    assert cumulative[0] == 0.0
    assert np.all(np.diff(cumulative) > 0)


def test_cumulative_miles_rescales_to_reported_total():
    """Mile markers must agree with the distance the router reported."""
    points = np.array([[40.0, -100.0], [40.0, -99.0], [40.0, -98.0]])
    cumulative = cumulative_miles(points, total_miles=250.0)
    assert cumulative[-1] == pytest.approx(250.0)
    assert cumulative[0] == 0.0


def test_cumulative_miles_handles_degenerate_input():
    assert len(cumulative_miles(np.empty((0, 2)))) == 0
    assert cumulative_miles(np.array([[40.0, -100.0]])).tolist() == [0.0]


def test_cumulative_miles_with_zero_length_route():
    """A route that never moves must not divide by zero when rescaling."""
    points = np.array([[40.0, -100.0], [40.0, -100.0]])
    assert cumulative_miles(points, total_miles=0.0).tolist() == [0.0, 0.0]


def test_resample_keeps_endpoints():
    points = np.column_stack([np.full(500, 40.0), np.linspace(-100.0, -90.0, 500)])
    cumulative = cumulative_miles(points)
    keep = resample_indices(cumulative, step_miles=10.0)
    assert keep[0] == 0
    assert keep[-1] == len(cumulative) - 1
    assert len(keep) < len(cumulative)


def test_resample_of_tiny_route_returns_everything():
    cumulative = np.array([0.0, 1.0])
    assert resample_indices(cumulative, step_miles=10.0).tolist() == [0, 1]


@pytest.mark.parametrize(
    "lat,lon",
    [
        (40.7128, -74.0060),  # New York
        (34.0522, -118.2437),  # Los Angeles
        (61.2181, -149.9003),  # Anchorage
        (21.3069, -157.8583),  # Honolulu
        (38.9072, -77.0369),  # Washington DC
        (24.5551, -81.7800),  # Key West, the southernmost continental city
        (25.7617, -80.1918),  # Miami
        (47.6062, -122.3321),  # Seattle
        (44.3876, -68.2039),  # Bar Harbor, Maine
    ],
)
def test_real_us_locations_are_accepted(lat, lon):
    """No legitimate US location may be rejected."""
    assert in_usa(lat, lon) is True


@pytest.mark.parametrize(
    "lat,lon",
    [
        (48.8566, 2.3522),  # Paris
        (19.4326, -99.1332),  # Mexico City
        (51.5074, -0.1278),  # London
        (-33.8688, 151.2093),  # Sydney
        (0.0, 0.0),  # Gulf of Guinea
        (55.7558, 37.6173),  # Moscow
    ],
)
def test_clearly_foreign_locations_are_rejected(lat, lon):
    assert in_usa(lat, lon) is False


@pytest.mark.parametrize(
    "lat,lon",
    [
        (43.6532, -79.3832),  # Toronto
        (32.5149, -117.0382),  # Tijuana
    ],
)
def test_near_border_foreign_points_are_accepted_by_design(lat, lon):
    """Documents the deliberate permissiveness of the bounding-box guard.

    These sit inside the contiguous-US rectangle. Tightening the test enough to
    exclude them also excludes real US territory, so named locations are
    filtered by the geocoder instead.
    """
    assert in_usa(lat, lon) is True
