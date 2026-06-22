"""Tests for the HeuristicScheduler and PriorityFairScheduler."""

from __future__ import annotations

from satsim.adapters.scheduler import HeuristicScheduler, PriorityFairScheduler
from satsim.domain.enums import Band, FleetId, Region, RejectReason, TrafficClass
from satsim.domain.models import (
    Beam,
    CandidateOption,
    ConstellationSnapshot,
    RegulatoryConstraints,
    RequestScore,
    Satellite,
    ScheduleCandidate,
    ServiceRequest,
)
from satsim.ports.scheduler import ResourceScheduler

_FLEET = FleetId.NEXT_GEN
_BAND = Band.KU
_REGION = Region.NA


def _snapshot(
    *capacities: float, region: Region = _REGION, band: Band = _BAND
) -> ConstellationSnapshot:
    beams = tuple(
        Beam(beam_id=f"b{i}", fleet=_FLEET, satellite_id="s", region=region, band=band,
             capacity_units=cap)
        for i, cap in enumerate(capacities)
    )
    return ConstellationSnapshot(step=0, satellites=(Satellite("s", _FLEET, beams),))


def _candidate(
    rid: str,
    *,
    score: float,
    cost: float,
    region: Region = _REGION,
    band: Band = _BAND,
    deadline: int = 10,
    arrival: int = 0,
    max_airtime: float | None = None,
    degradable: bool = False,
) -> ScheduleCandidate:
    request = ServiceRequest(
        request_id=rid, traffic_class=TrafficClass.MESSAGING, region=region,
        size_bytes=1, link_quality=0.9, urgency=0.0, arrival_step=arrival, deadline_step=deadline,
    )
    option = CandidateOption(
        fleet=_FLEET, band=band,
        constraints=RegulatoryConstraints(max_airtime_units=max_airtime),
    )
    return ScheduleCandidate(
        request=request,
        options=(option,),
        scoring=RequestScore(score=score, estimated_cost_units=cost, delivery_probability=0.9),
        degradable=degradable,
    )


def test_both_satisfy_port() -> None:
    assert isinstance(HeuristicScheduler(), ResourceScheduler)
    assert isinstance(PriorityFairScheduler(), ResourceScheduler)


def test_high_score_served_first_under_scarcity() -> None:
    sched = HeuristicScheduler()
    snap = _snapshot(5.0)  # only room for one 5-unit request
    result = sched.schedule(
        [_candidate("low", score=0.1, cost=5.0), _candidate("high", score=0.9, cost=5.0)],
        snap, step=0,
    )
    assert [r.request_id for r in result.served] == ["high"]
    assert [r.request_id for r in result.deferred] == ["low"]


def test_expired_request_dropped() -> None:
    sched = HeuristicScheduler()
    result = sched.schedule(
        [_candidate("old", score=0.9, cost=1.0, arrival=0, deadline=0)], _snapshot(10.0), step=1
    )
    assert result.served == ()
    assert result.dropped[0].reason is RejectReason.DEADLINE_MISSED


def test_wrong_region_is_deferred_not_served() -> None:
    sched = HeuristicScheduler()
    snap = _snapshot(10.0, region=Region.EU)  # beam illuminates EU
    result = sched.schedule([_candidate("na", score=0.9, cost=1.0, region=Region.NA)], snap, step=0)
    assert result.served == ()
    assert [r.request_id for r in result.deferred] == ["na"]


def test_regulatory_airtime_cap_blocks_oversized() -> None:
    sched = HeuristicScheduler()
    snap = _snapshot(100.0)
    # Request needs 8 units but the region caps airtime at 4 -> cannot be served.
    result = sched.schedule(
        [_candidate("big", score=0.9, cost=8.0, max_airtime=4.0)], snap, step=0
    )
    assert result.served == ()
    assert [r.request_id for r in result.deferred] == ["big"]


def test_priority_fair_cap_prevents_monopolization() -> None:
    # One oversized (10-unit) high-score request + two small ones, on a single 12-unit beam.
    big = _candidate("big", score=0.9, cost=10.0)
    small1 = _candidate("s1", score=0.5, cost=1.0)
    small2 = _candidate("s2", score=0.5, cost=1.0)
    snap = _snapshot(12.0)

    # Heuristic: the big request grabs the beam first.
    heuristic = HeuristicScheduler().schedule([big, small1, small2], snap, step=0)
    assert "big" in {r.request_id for r in heuristic.served}

    # PriorityFair (cap 6): the 10-unit request can't fit within the cap -> deferred;
    # the small requests are served instead.
    fair = PriorityFairScheduler(max_units_per_request=6.0).schedule(
        [big, small1, small2], snap, step=0
    )
    served_ids = {r.request_id for r in fair.served}
    assert "big" not in served_ids
    assert served_ids == {"s1", "s2"}


def test_utilization_and_fairness_reported() -> None:
    sched = HeuristicScheduler()
    snap = _snapshot(10.0)
    result = sched.schedule(
        [_candidate("a", score=0.6, cost=2.0), _candidate("b", score=0.5, cost=2.0)], snap, step=0
    )
    assert len(result.served) == 2
    assert result.utilization == 0.4  # 4 of 10 units
    assert result.fairness_index == 1.0  # equal allocations


def test_degradable_request_gets_reduced_allocation() -> None:
    # Beam has 4 units; a degradable request needing 10 is served degraded with the 4 available.
    sched = HeuristicScheduler()
    snap = _snapshot(4.0)
    result = sched.schedule(
        [_candidate("photo", score=0.3, cost=10.0, degradable=True)], snap, step=0
    )
    assert [r.request_id for r in result.served] == ["photo"]
    assert result.allocations[0].degraded is True
    assert result.allocations[0].allocated_units == 4.0


def test_non_degradable_request_is_deferred_not_partial() -> None:
    sched = HeuristicScheduler()
    snap = _snapshot(4.0)
    result = sched.schedule(
        [_candidate("msg", score=0.3, cost=10.0, degradable=False)], snap, step=0
    )
    assert result.served == ()
    assert [r.request_id for r in result.deferred] == ["msg"]


def test_degradation_respects_minimum_fraction() -> None:
    # Only 1 unit free for a 10-unit degradable request: below the 25% floor -> deferred.
    sched = HeuristicScheduler()
    snap = _snapshot(1.0)
    result = sched.schedule(
        [_candidate("photo", score=0.3, cost=10.0, degradable=True)], snap, step=0
    )
    assert result.served == ()
    assert [r.request_id for r in result.deferred] == ["photo"]


def test_empty_candidates_is_clean() -> None:
    result = HeuristicScheduler().schedule([], _snapshot(10.0), step=0)
    assert result.served == () and result.deferred == () and result.dropped == ()
    assert result.utilization == 0.0
    assert result.fairness_index == 1.0
