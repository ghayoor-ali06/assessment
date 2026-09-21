"""Optimiser correctness, including an exhaustive brute-force cross-check."""

from __future__ import annotations

import random
from functools import lru_cache

import pytest

from routing.services.optimizer import (
    Candidate,
    InfeasibleRoute,
    cluster_candidates,
    plan_fuel_stops,
)


def brute_force_min_cost(miles, prices, total_miles, range_miles):
    """Exact minimum cost by dynamic programming over integer fuel levels.

    Deliberately written in a different style from the greedy under test: it
    enumerates every purchase amount at every station rather than reasoning
    about which station comes next. ``mpg`` is fixed at 1 so a mile of range
    costs exactly one gallon, which keeps the state space integral.

    Assumes a station at mile 0 (the caller guarantees it), so both
    implementations face the same starting conditions.
    """
    n = len(miles)

    @lru_cache(maxsize=None)
    def best(index: int, fuel: int) -> float:
        cheapest = float("inf")
        here = miles[index]
        price = prices[index]
        for purchase in range(0, range_miles - fuel + 1):
            tank = fuel + purchase
            spent = purchase * price
            if total_miles - here <= tank:
                cheapest = min(cheapest, spent)
            for j in range(index + 1, n):
                leg = miles[j] - here
                if leg > tank:
                    break
                onward = best(j, tank - leg)
                if onward < float("inf"):
                    cheapest = min(cheapest, spent + onward)
        return cheapest

    result = best(0, 0)
    best.cache_clear()
    return result


@pytest.mark.parametrize("seed", range(120))
def test_greedy_matches_brute_force(seed):
    """The greedy plan must cost exactly what exhaustive search says it should.

    This is the test that catches advance-rule bugs: an earlier draft that drove
    to the farthest reachable station instead of the cheapest passed every
    hand-written case but failed here.
    """
    rng = random.Random(seed)
    range_miles = rng.choice([8, 10, 12])
    count = rng.randint(2, 7)

    # Station at mile 0, then strictly increasing positions no further apart
    # than the range so the instance is always feasible.
    miles = [0]
    for _ in range(count - 1):
        miles.append(miles[-1] + rng.randint(1, range_miles))
    total_miles = miles[-1] + rng.randint(0, range_miles)
    prices = [rng.randint(1, 9) for _ in miles]

    expected = brute_force_min_cost(tuple(miles), tuple(prices), total_miles, range_miles)

    candidates = [Candidate(mile=float(m), price=float(p)) for m, p in zip(miles, prices)]
    plan = plan_fuel_stops(candidates, float(total_miles), range_miles=float(range_miles), mpg=1.0)

    assert plan.total_cost == pytest.approx(expected, abs=1e-6)
    # Every mile driven is paid for.
    assert plan.total_gallons == pytest.approx(total_miles, abs=1e-6)


def test_total_gallons_is_distance_over_mpg():
    candidates = [Candidate(mile=0.0, price=3.0), Candidate(mile=200.0, price=2.5)]
    plan = plan_fuel_stops(candidates, 400.0, range_miles=500.0, mpg=10.0)
    assert plan.total_gallons == pytest.approx(40.0)


def test_prefers_cheaper_station_further_along():
    """With a cheap station in range, buy only enough to reach it."""
    candidates = [Candidate(mile=0.0, price=5.0), Candidate(mile=100.0, price=1.0)]
    plan = plan_fuel_stops(candidates, 300.0, range_miles=500.0, mpg=1.0)
    # 100 miles at $5, the remaining 200 at $1.
    assert plan.total_cost == pytest.approx(100 * 5 + 200 * 1)
    assert [round(s.mile) for s in plan.stops] == [0, 100]


def test_never_buys_more_than_needed_to_finish():
    """A dirt-cheap station near the end must not trigger a full tank."""
    candidates = [Candidate(mile=0.0, price=4.0), Candidate(mile=90.0, price=1.0)]
    plan = plan_fuel_stops(candidates, 100.0, range_miles=500.0, mpg=1.0)
    assert plan.total_gallons == pytest.approx(100.0)
    assert plan.stops[-1].gallons == pytest.approx(10.0)


def test_first_stop_pays_for_the_opening_miles():
    """The tank starts empty, so mile 0 to the first station must still be paid."""
    candidates = [Candidate(mile=40.0, price=3.0)]
    plan = plan_fuel_stops(candidates, 100.0, range_miles=500.0, mpg=10.0)

    assert len(plan.stops) == 1
    stop = plan.stops[0]
    # Reported at the real station, not at a phantom stop on the origin.
    assert stop.mile == pytest.approx(40.0)
    assert stop.covers_origin_miles == pytest.approx(40.0)
    # All 100 miles are charged, including the 40 driven before the stop.
    assert plan.total_gallons == pytest.approx(10.0)
    assert plan.total_cost == pytest.approx(30.0)


def test_station_at_origin_has_nothing_to_backfill():
    candidates = [Candidate(mile=0.0, price=3.0)]
    plan = plan_fuel_stops(candidates, 100.0, range_miles=500.0, mpg=10.0)
    assert plan.stops[0].covers_origin_miles == 0.0


def test_opening_miles_charged_when_station_sits_on_destination():
    """Degenerate case: the only station is at the end of the route."""
    candidates = [Candidate(mile=100.0, price=3.0)]
    plan = plan_fuel_stops(candidates, 100.0, range_miles=500.0, mpg=10.0)
    assert plan.total_gallons == pytest.approx(10.0)
    assert plan.total_cost == pytest.approx(30.0)


@pytest.mark.parametrize("seed", range(60))
def test_offset_first_station_still_pays_for_every_mile(seed):
    """Shifting every station down the route must not lose or invent gallons."""
    rng = random.Random(seed)
    range_miles = 500.0
    offset = rng.uniform(1.0, 200.0)
    miles = [offset]
    for _ in range(rng.randint(1, 5)):
        miles.append(miles[-1] + rng.uniform(1.0, range_miles))
    total = miles[-1] + rng.uniform(0.0, range_miles)
    candidates = [Candidate(mile=m, price=rng.uniform(2.5, 5.0)) for m in miles]

    plan = plan_fuel_stops(candidates, total, range_miles=range_miles, mpg=10.0)

    assert plan.total_gallons == pytest.approx(total / 10.0, rel=1e-9)
    assert sum(s.gallons for s in plan.stops) == pytest.approx(total / 10.0, rel=1e-9)
    assert sum(s.cost for s in plan.stops) == pytest.approx(plan.total_cost, rel=1e-9)


def test_zero_length_trip_costs_nothing():
    plan = plan_fuel_stops([Candidate(mile=0.0, price=3.0)], 0.0)
    assert plan.stops == []
    assert plan.total_cost == 0.0
    assert plan.total_gallons == 0.0


def test_zero_length_trip_with_no_stations():
    assert plan_fuel_stops([], 0.0).total_cost == 0.0


def test_no_candidates_is_infeasible():
    with pytest.raises(InfeasibleRoute) as excinfo:
        plan_fuel_stops([], 100.0)
    assert excinfo.value.reason == "no_stations_in_corridor"


def test_gap_exactly_at_range_is_feasible():
    candidates = [Candidate(mile=0.0, price=3.0), Candidate(mile=500.0, price=3.0)]
    plan = plan_fuel_stops(candidates, 600.0, range_miles=500.0, mpg=10.0)
    assert plan.total_gallons == pytest.approx(60.0)


def test_gap_just_over_range_is_infeasible():
    candidates = [Candidate(mile=0.0, price=3.0), Candidate(mile=500.1, price=3.0)]
    with pytest.raises(InfeasibleRoute) as excinfo:
        plan_fuel_stops(candidates, 600.0, range_miles=500.0, mpg=10.0)
    assert excinfo.value.gap["gap_miles"] == pytest.approx(500.1, abs=0.05)


def test_unreachable_first_station_reports_origin_gap():
    with pytest.raises(InfeasibleRoute) as excinfo:
        plan_fuel_stops([Candidate(mile=600.0, price=3.0)], 700.0, range_miles=500.0)
    assert excinfo.value.gap["from_mile"] == 0.0


def test_unreachable_destination_reports_tail_gap():
    """Mirrors the real Seattle to Los Angeles failure."""
    with pytest.raises(InfeasibleRoute) as excinfo:
        plan_fuel_stops([Candidate(mile=258.0, price=3.0)], 1135.0, range_miles=500.0)
    assert excinfo.value.gap["from_mile"] == pytest.approx(258.0)
    assert excinfo.value.gap["gap_miles"] == pytest.approx(877.0)


def test_rejects_non_positive_parameters():
    candidates = [Candidate(mile=0.0, price=3.0)]
    with pytest.raises(ValueError):
        plan_fuel_stops(candidates, 100.0, range_miles=0)
    with pytest.raises(ValueError):
        plan_fuel_stops(candidates, 100.0, mpg=0)


def test_long_route_needs_multiple_stops():
    """1,400 miles at 500 mi range cannot be done in fewer than three fills."""
    candidates = [Candidate(mile=float(m), price=3.0) for m in range(0, 1400, 100)]
    plan = plan_fuel_stops(candidates, 1400.0, range_miles=500.0, mpg=10.0)
    assert len(plan.stops) >= 3
    assert plan.total_gallons == pytest.approx(140.0)


def test_clustering_keeps_cheapest_per_bin():
    candidates = [
        Candidate(mile=0.0, price=4.0),
        Candidate(mile=5.0, price=2.0),
        Candidate(mile=9.0, price=3.0),
        Candidate(mile=30.0, price=5.0),
    ]
    clustered = cluster_candidates(candidates, bin_miles=25.0)
    assert [(c.mile, c.price) for c in clustered] == [(5.0, 2.0), (30.0, 5.0)]


def test_clustering_disabled_returns_sorted_input():
    candidates = [Candidate(mile=9.0, price=3.0), Candidate(mile=0.0, price=4.0)]
    assert [c.mile for c in cluster_candidates(candidates, bin_miles=0)] == [0.0, 9.0]


def test_average_price_is_cost_weighted():
    candidates = [Candidate(mile=0.0, price=2.0), Candidate(mile=100.0, price=4.0)]
    plan = plan_fuel_stops(candidates, 200.0, range_miles=500.0, mpg=1.0)
    assert plan.average_price == pytest.approx(plan.total_cost / plan.total_gallons)
