"""US place-name lookup backed by a committed Census gazetteer extract.

The supplied fuel-price CSV identifies each truck stop only by city and state,
with no coordinates, so every station has to be placed on the map before any
routing can happen. Doing that over the network at request time would be slow
and rude; instead ``build_station_data`` bakes the answers into
``data/us_places.csv`` and this module reads them back.

The same index also resolves user-supplied "City, ST" input, which is why a
typical API request makes no geocoding calls at all.
"""

from __future__ import annotations

import csv
import re
import threading
import unicodedata
from pathlib import Path

from django.conf import settings

STATE_ABBREVIATIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
}

# The source price file also covers Canada, and users naturally try Canadian
# cities. Recognising these lets the API say "US only" instead of silently
# matching a same-named US place (Toronto, Ontario -> Ontario, California).
CANADIAN_REGIONS = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
    "ALBERTA", "BRITISH COLUMBIA", "MANITOBA", "NEW BRUNSWICK",
    "NEWFOUNDLAND", "NEWFOUNDLAND AND LABRADOR", "NORTHWEST TERRITORIES",
    "NOVA SCOTIA", "NUNAVUT", "ONTARIO", "PRINCE EDWARD ISLAND", "QUEBEC",
    "SASKATCHEWAN", "YUKON", "CANADA",
}

STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL",
    "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY",
    "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO",
    "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
}

# Legal-status words the Census appends to place names ("Abanda CDP",
# "Canton charter township"). They carry no identifying information and never
# appear in the fuel-price CSV, so they are stripped before matching.
_STATUS_WORDS = re.compile(
    r"\b(CDP|CCD|CITY AND BOROUGH|CITY|TOWN|VILLAGE|BOROUGH|MUNICIPALITY"
    r"|CHARTER TOWNSHIP|TOWNSHIP|PLANTATION|UNIFIED GOVERNMENT"
    r"|CONSOLIDATED GOVERNMENT|METROPOLITAN GOVERNMENT|METRO GOVERNMENT"
    r"|URBAN COUNTY GOVERNMENT|GOVERNMENT|RESERVATION)\b"
)
_BALANCE = re.compile(r"\s*\(BALANCE\)\s*")


def normalize_place(name: str) -> str:
    """Fold a place name to a comparable key.

    Handles the differences that stop the fuel CSV and the Census files from
    lining up: accents (Cañon City), abbreviations (St./Ste./Mt./Ft.),
    punctuation, hyphenation and Census status suffixes.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    folded = folded.upper().strip()
    folded = folded.replace(".", "").replace(",", "").replace("'", "")
    folded = _BALANCE.sub(" ", folded)
    folded = re.sub(r"[-/]", " ", folded)
    folded = _STATUS_WORDS.sub(" ", folded)
    folded = re.sub(r"\bSTE\b", "SAINTE", folded)
    folded = re.sub(r"\bST\b", "SAINT", folded)
    folded = re.sub(r"\bMT\b", "MOUNT", folded)
    folded = re.sub(r"\bFT\b", "FORT", folded)
    return re.sub(r"\s+", " ", folded).strip()


def squash(name: str) -> str:
    """Normalised key with spaces removed (DeForest vs De Forest)."""
    return normalize_place(name).replace(" ", "")


def places_path() -> Path:
    return Path(settings.DATA_DIR) / "us_places.csv"


class PlaceIndex:
    """Lookup of (place name, state) to coordinates, with graded fallbacks."""

    def __init__(self) -> None:
        self.exact: dict[tuple[str, str], tuple[float, float]] = {}
        self.squashed: dict[tuple[str, str], tuple[float, float]] = {}
        self.prefix: dict[tuple[str, str], tuple[float, float]] = {}

    def add(self, name: str, state: str, lat: float, lon: float) -> None:
        state = state.strip().upper()
        key = normalize_place(name)
        if not key:
            return
        point = (lat, lon)
        # First writer wins: the places file is loaded before county
        # subdivisions, so an incorporated place outranks a same-named township.
        self.exact.setdefault((key, state), point)
        self.squashed.setdefault((squash(name), state), point)
        # Consolidated governments appear as "Nashville-Davidson metropolitan
        # government"; indexing the leading token lets "Nashville" find them.
        self.prefix.setdefault((key.split(" ")[0], state), point)

    def lookup(self, city: str, state: str) -> tuple[float, float] | None:
        state = state.strip().upper()
        key = normalize_place(city)
        if not key:
            return None
        found = (
            self.exact.get((key, state))
            or self.squashed.get((squash(city), state))
            or self.prefix.get((key, state))
        )
        if found is None and " " in key:
            found = self.exact.get((key.split(" ")[0], state))
        return found

    def __len__(self) -> int:
        return len(self.exact)


_index: PlaceIndex | None = None
_lock = threading.Lock()


def get_place_index() -> PlaceIndex:
    """Load ``data/us_places.csv`` once per process."""
    global _index
    if _index is not None:
        return _index
    with _lock:
        if _index is not None:
            return _index
        index = PlaceIndex()
        path = places_path()
        if path.exists():
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    try:
                        index.add(row["name"], row["state"], float(row["lat"]), float(row["lon"]))
                    except (KeyError, TypeError, ValueError):
                        continue
        _index = index
        return _index


def reset_place_index() -> None:
    """Drop the cached index (used by tests)."""
    global _index
    with _lock:
        _index = None
