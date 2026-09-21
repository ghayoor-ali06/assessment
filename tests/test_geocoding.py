"""Location resolution. The offline paths must never touch the network."""

from __future__ import annotations

import pytest

from routing.services import geocoding
from routing.services.gazetteer import PlaceIndex, reset_place_index
from routing.services.geocoding import GeocodingError, geocode


@pytest.fixture(autouse=True)
def offline_place_index(monkeypatch):
    """A tiny in-memory gazetteer, so tests do not depend on the built CSV."""
    index = PlaceIndex()
    index.add("Denver", "CO", 39.7392, -104.9903)
    index.add("Chicago", "IL", 41.8781, -87.6298)
    index.add("Kansas City", "MO", 39.0997, -94.5786)
    index.add("Nashville-Davidson metropolitan government (balance)", "TN", 36.1627, -86.7816)
    index.add("DeForest", "WI", 43.2447, -89.3435)
    monkeypatch.setattr(geocoding, "get_place_index", lambda: index)
    yield
    reset_place_index()


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if a test reaches the remote geocoder unexpectedly."""

    def _explode(*args, **kwargs):
        raise AssertionError("remote geocoding should not be called here")

    monkeypatch.setattr(geocoding, "_geocode_remote", _explode)


@pytest.mark.parametrize(
    "query",
    ["Denver, CO", "denver, co", "  Denver ,  CO  ", "Denver, Colorado", "Denver, CO, USA", "Denver CO"],
)
def test_city_state_forms_resolve_offline(query, no_network):
    location = geocode(query)
    assert location.source == "gazetteer"
    assert location.latitude == pytest.approx(39.7392)


def test_multi_word_city_resolves(no_network):
    assert geocode("Kansas City, MO").longitude == pytest.approx(-94.5786)


def test_consolidated_government_resolves_by_prefix(no_network):
    """'Nashville, TN' must find 'Nashville-Davidson metropolitan government'."""
    assert geocode("Nashville, TN").latitude == pytest.approx(36.1627)


def test_spacing_variant_resolves(no_network):
    """'De Forest, WI' and 'DeForest, WI' are the same place."""
    assert geocode("De Forest, WI").latitude == pytest.approx(43.2447)


@pytest.mark.parametrize(
    "query,lat,lon",
    [
        ("39.7392,-104.9903", 39.7392, -104.9903),
        ("39.7392, -104.9903", 39.7392, -104.9903),
        ("  40.0 / -100.0 ", 40.0, -100.0),
        ("40 -100", 40.0, -100.0),
    ],
)
def test_coordinate_forms(query, lat, lon, no_network):
    location = geocode(query)
    assert location.source == "coordinates"
    assert (location.latitude, location.longitude) == pytest.approx((lat, lon))


def test_coordinates_outside_usa_are_rejected(no_network):
    with pytest.raises(GeocodingError, match="outside the USA"):
        geocode("48.8566,2.3522")


@pytest.mark.parametrize("query", ["999,999", "91,0", "0,181", "-91,-100"])
def test_out_of_range_coordinates_are_rejected(query, no_network):
    with pytest.raises(GeocodingError, match="out of range"):
        geocode(query)


@pytest.mark.parametrize("query", ["Toronto, Ontario", "Calgary, AB", "Vancouver, British Columbia"])
def test_canadian_locations_are_rejected(query, no_network):
    """Without this the geocoder quietly matches a US namesake."""
    with pytest.raises(GeocodingError, match="Canada"):
        geocode(query)


@pytest.mark.parametrize("query", ["", "   ", None])
def test_blank_input_is_rejected(query, no_network):
    with pytest.raises(GeocodingError, match="required"):
        geocode(query)


def test_unknown_place_falls_through_to_remote(monkeypatch):
    called = {}

    def _fake_remote(query):
        called["query"] = query
        return 41.0, -95.0, "Somewhere, NE, United States"

    monkeypatch.setattr(geocoding, "_geocode_remote", _fake_remote)
    location = geocode("1600 Pennsylvania Ave")
    assert called["query"] == "1600 Pennsylvania Ave"
    assert location.source == "nominatim"


def test_remote_result_outside_usa_is_rejected(monkeypatch):
    monkeypatch.setattr(
        geocoding, "_geocode_remote", lambda q: (48.8566, 2.3522, "Paris, France")
    )
    with pytest.raises(GeocodingError, match="outside the USA"):
        geocode("somewhere odd")


def test_remote_geocoding_can_be_disabled(settings):
    """With the network path off, unresolvable input fails instead of hanging."""
    settings.ENABLE_REMOTE_GEOCODING = False
    with pytest.raises(GeocodingError, match="City, ST"):
        geocode("1600 Pennsylvania Ave")


def test_error_message_names_the_offending_field(no_network):
    with pytest.raises(GeocodingError, match="finish"):
        geocode("48.8566,2.3522", label="finish")
