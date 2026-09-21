"""Cost-optimal fuel planning along a fixed route.

This is the classic *gas station problem* (Khuller, Malekian & Mestre, 2007).
The route is fixed, so the only decision left is where to stop and how many
gallons to buy at each stop so the total bill is as small as possible.

Fuel model (documented assumption, see README):
    The tank starts empty, so every mile driven is paid for. Total gallons
    burned is always ``distance / mpg``; the optimiser only decides how that
    spend is distributed across stations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Tolerance for float comparisons on mile/gallon quantities.
EPS = 1e-9


class InfeasibleRoute(Exception):
    """No legal fuelling plan exists for this route.

    Raised when two consecutive usable stations (or the origin/destination and
    their nearest station) are further apart than the vehicle's range. Carries
    a machine-readable ``gap`` so the API can explain exactly where the route
    breaks down instead of just failing.
    """

    def __init__(self, reason: str, detail: str, gap: dict[str, float] | None = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.gap = gap


@dataclass(frozen=True)
class Candidate:
    """A station that is close enough to the route to be worth stopping at."""

    mile: float  # distance from the origin, along the route
    price: float  # USD per gallon
    station: Any = None  # opaque payload handed back to the caller


@dataclass
class FuelStop:
    mile: float
    gallons: float
    price: float
    cost: float
    station: Any = None
    # Miles driven before this stop that it also pays for. Non-zero only on the
    # first stop, when the route's first station is not at the origin.
    covers_origin_miles: float = 0.0


@dataclass
class FuelPlan:
    stops: list[FuelStop] = field(default_factory=list)
    total_cost: float = 0.0
    total_gallons: float = 0.0

    @property
    def average_price(self) -> float:
        return self.total_cost / self.total_gallons if self.total_gallons > EPS else 0.0


def cluster_candidates(candidates: list[Candidate], bin_miles: float) -> list[Candidate]:
    """Keep only the cheapest candidate within each ``bin_miles`` stretch.

    Truck stops cluster heavily around interchanges, and a strictly optimal plan
    over the raw set happily prescribes a 0.4-gallon purchase at one station
    followed by another 3 miles later. That is correct arithmetic and useless
    advice. Thinning to the cheapest option per bin removes the noise; measured
    cost impact on real routes is nil to a few cents.

    Pass ``bin_miles <= 0`` to disable.
    """
    if bin_miles <= 0 or not candidates:
        return sorted(candidates, key=lambda c: c.mile)

    best: dict[int, Candidate] = {}
    for candidate in candidates:
        key = int(candidate.mile // bin_miles)
        incumbent = best.get(key)
        if incumbent is None or candidate.price < incumbent.price:
            best[key] = candidate
    return sorted(best.values(), key=lambda c: c.mile)


def _assert_reachable(candidates: list[Candidate], total_miles: float, range_miles: float) -> None:
    """Raise InfeasibleRoute if any leg exceeds the vehicle's range."""
    if not candidates:
        raise InfeasibleRoute(
            "no_stations_in_corridor",
            "No fuel stations were found close enough to this route.",
        )

    first = candidates[0]
    if first.mile > range_miles:
        raise InfeasibleRoute(
            "no_station_in_range",
            f"The nearest station on this route is {first.mile:.0f} mi from the origin, "
            f"beyond the {range_miles:.0f} mi range.",
            {"from_mile": 0.0, "to_mile": round(first.mile, 1), "gap_miles": round(first.mile, 1)},
        )

    for current, following in zip(candidates, candidates[1:]):
        gap = following.mile - current.mile
        if gap > range_miles:
            raise InfeasibleRoute(
                "no_station_in_range",
                f"{gap:.0f} mi gap between the stations at mile {current.mile:.0f} and "
                f"mile {following.mile:.0f}; exceeds the {range_miles:.0f} mi range.",
                {
                    "from_mile": round(current.mile, 1),
                    "to_mile": round(following.mile, 1),
                    "gap_miles": round(gap, 1),
                },
            )

    last = candidates[-1]
    tail = total_miles - last.mile
    if tail > range_miles:
        raise InfeasibleRoute(
            "no_station_in_range",
            f"{tail:.0f} mi gap between the last station (mile {last.mile:.0f}) and the "
            f"destination (mile {total_miles:.0f}); exceeds the {range_miles:.0f} mi range.",
            {
                "from_mile": round(last.mile, 1),
                "to_mile": round(total_miles, 1),
                "gap_miles": round(tail, 1),
            },
        )


def plan_fuel_stops(
    candidates: list[Candidate],
    total_miles: float,
    range_miles: float = 500.0,
    mpg: float = 10.0,
) -> FuelPlan:
    """Cheapest way to buy the fuel this trip burns.

    ``candidates`` must be sorted by ``mile``. Returns an empty plan for a
    zero-length trip; raises :class:`InfeasibleRoute` when the route cannot be
    completed within ``range_miles``.
    """
    if range_miles <= 0 or mpg <= 0:
        raise ValueError("range_miles and mpg must be positive")

    if total_miles <= EPS:
        return FuelPlan()

    candidates = sorted(candidates, key=lambda c: c.mile)
    _assert_reachable(candidates, total_miles, range_miles)

    nodes = list(candidates)

    # The tank starts empty, but the first station is rarely at mile zero. The
    # driver covers those opening miles on a reserve and settles up on arrival,
    # at that station's price. Modelling it this way keeps every mile paid for
    # without inventing a phantom stop at the origin.
    reserve_miles = nodes[0].mile

    plan = FuelPlan()
    fuel = reserve_miles  # miles of range currently in the tank
    position = 0.0
    index = 0

    while True:
        node = nodes[index]
        fuel -= node.mile - position
        position = node.mile
        if fuel < -EPS:  # pragma: no cover - guarded by _assert_reachable
            raise InfeasibleRoute("ran_dry", "Ran out of fuel before reaching a station.")

        remaining = total_miles - node.mile

        # The first station within range that is strictly cheaper than here.
        cheaper_ahead = None
        for ahead in range(index + 1, len(nodes)):
            if nodes[ahead].mile - node.mile > range_miles:
                break
            if nodes[ahead].price < node.price:
                cheaper_ahead = ahead
                break

        if remaining <= range_miles:
            # The destination is reachable on one tank: never buy more than the
            # miles left, and stop early if somewhere cheaper comes first.
            target = remaining
            if cheaper_ahead is not None:
                distance_to_cheaper = nodes[cheaper_ahead].mile - node.mile
                if distance_to_cheaper < remaining:
                    target = distance_to_cheaper
        elif cheaper_ahead is not None:
            # Buy just enough to reach the cheaper station.
            target = nodes[cheaper_ahead].mile - node.mile
        else:
            # Nothing cheaper within range: this is the best price available, so
            # fill the tank.
            target = range_miles

        purchase = target - fuel
        if purchase > EPS:
            gallons = purchase / mpg
            cost = gallons * node.price
            fuel += purchase
            plan.total_gallons += gallons
            plan.total_cost += cost
            plan.stops.append(
                FuelStop(
                    mile=node.mile,
                    gallons=gallons,
                    price=node.price,
                    cost=cost,
                    station=node.station,
                )
            )

        if remaining <= fuel + EPS:
            break

        if cheaper_ahead is not None:
            index = cheaper_ahead
        else:
            # Tank is full and nothing cheaper is in range, so drive to the
            # cheapest station we can still reach. Picking the *farthest*
            # reachable station instead is a subtle and expensive mistake.
            reachable = [
                ahead
                for ahead in range(index + 1, len(nodes))
                if nodes[ahead].mile - node.mile <= fuel + EPS
            ]
            if not reachable:  # pragma: no cover - guarded by _assert_reachable
                raise InfeasibleRoute("no_station_in_range", "No reachable station ahead.")
            index = min(reachable, key=lambda k: (nodes[k].price, -nodes[k].mile))

    if reserve_miles > EPS:
        # Charge the opening miles at the first station's price. Arriving with
        # an empty tank guarantees a purchase there, except in the degenerate
        # case where that station sits exactly on the destination.
        gallons = reserve_miles / mpg
        if not plan.stops:
            first = nodes[0]
            plan.stops.append(
                FuelStop(mile=first.mile, gallons=0.0, price=first.price, cost=0.0,
                         station=first.station)
            )
        stop = plan.stops[0]
        cost = gallons * stop.price
        stop.gallons += gallons
        stop.cost += cost
        stop.covers_origin_miles = reserve_miles
        plan.total_gallons += gallons
        plan.total_cost += cost

    return plan
