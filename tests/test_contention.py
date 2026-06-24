"""Tests for the ContentionMacScheduler (slotted random access / ALOHA collisions)."""

from __future__ import annotations

from satsim.adapters.mac import ContentionMacScheduler
from satsim.adapters.telemetry import InMemoryTelemetrySink
from satsim.config import MacConfig, SimulationConfig
from satsim.domain.enums import Band, FleetId, Region, RejectReason, SchedulerKind, TrafficClass
from satsim.domain.models import (
    Beam,
    CandidateOption,
    ConstellationSnapshot,
    RequestScore,
    Satellite,
    ScheduleCandidate,
    ServiceRequest,
)
from satsim.loop import build_simulation
from satsim.ports.scheduler import ResourceScheduler
from satsim.rng import Rng

_FLEET = FleetId.NEXT_GEN
_BAND = Band.KU
_REGION = Region.NA

# 256QAM (link >= 0.8) carries 8 bits/symbol; at 1000 symbols/RB one RB = 1000 bytes.
_ONE_RB = MacConfig(slots_per_step=1, subchannels=1, symbols_per_rb=1000)  # a single-RB beam
_BIG = MacConfig(slots_per_step=50, subchannels=1, symbols_per_rb=1000)    # 50-RB beam


def _snapshot(capacity: float, *, beams: int = 1) -> ConstellationSnapshot:
    made = tuple(Beam(f"b{i}", _FLEET, "s", _REGION, _BAND, capacity) for i in range(beams))
    return ConstellationSnapshot(step=0, satellites=(Satellite("s", _FLEET, made),))


def _candidate(
    rid: str, *, size: int = 1000, link: float = 0.9, deadline: int = 10
) -> ScheduleCandidate:
    request = ServiceRequest(
        request_id=rid, traffic_class=TrafficClass.MESSAGING, region=_REGION, size_bytes=size,
        link_quality=link, urgency=0.0, arrival_step=0, deadline_step=deadline,
    )
    return ScheduleCandidate(
        request=request,
        options=(CandidateOption(fleet=_FLEET, band=_BAND),),
        scoring=RequestScore(score=0.5, estimated_cost_units=1.0, delivery_probability=link),
    )


def test_satisfies_port() -> None:
    assert isinstance(ContentionMacScheduler(_ONE_RB, Rng(0)), ResourceScheduler)


def test_single_ue_succeeds() -> None:
    sched = ContentionMacScheduler(_BIG, Rng(0))
    result = sched.schedule([_candidate("solo")], _snapshot(50.0), step=0)
    assert [r.request_id for r in result.served] == ["solo"]
    assert result.allocations[0].allocated_units == 1.0  # one RB at 256QAM


def test_collision_defers_all_contenders() -> None:
    # Two UEs, one shared RB: both must pick RB 0, collide, and defer (none served).
    sched = ContentionMacScheduler(_ONE_RB, Rng(0))
    result = sched.schedule([_candidate("a"), _candidate("b")], _snapshot(1.0), step=0)
    assert result.served == ()
    assert {r.request_id for r in result.deferred} == {"a", "b"}
    assert result.utilization == 0.0  # the collided RB carried nothing


def test_overload_collapses_throughput() -> None:
    # Far more UEs than RBs -> collisions dominate -> most defer, throughput is capped well
    # below the contender count (the classic ALOHA collapse a granted scheduler would hide).
    sched = ContentionMacScheduler(MacConfig(slots_per_step=4, subchannels=1, symbols_per_rb=1000),
                                   Rng(7))
    cands = [_candidate(f"u{i:02d}") for i in range(40)]
    result = sched.schedule(cands, _snapshot(4.0), step=0)
    assert len(result.served) <= 4          # at most one UE per collision-free RB
    assert len(result.served) < len(cands)  # collisions forced deferrals
    assert result.deferred


def test_capture_capacity_allows_sharing() -> None:
    # With capture_capacity=2, two UEs on the one shared RB are both recovered (spreading codes),
    # and the RB's airtime is split between them.
    cfg = MacConfig(slots_per_step=1, subchannels=1, symbols_per_rb=1000, capture_capacity=2)
    result = ContentionMacScheduler(cfg, Rng(0)).schedule(
        [_candidate("a"), _candidate("b")], _snapshot(1.0), step=0
    )
    assert {r.request_id for r in result.served} == {"a", "b"}
    assert all(a.allocated_units == 0.5 for a in result.allocations)  # one RB split two ways
    assert result.utilization == 1.0  # airtime conserved: the shared RB is fully used


def test_capture_capacity_reduces_deferrals() -> None:
    # Same random selections (same seed) under increasing capture: collisions that overloaded a
    # pure-ALOHA RB are now recoverable, so deferrals fall and served rises monotonically.
    def _cfg(capture: int) -> MacConfig:
        return MacConfig(
            slots_per_step=8, subchannels=1, symbols_per_rb=1000, capture_capacity=capture
        )

    # 12 UEs contending over an 8-RB beam: moderate over-subscription so occupancy spans 1..4.
    cands = [_candidate(f"u{i:02d}") for i in range(12)]
    snap = _snapshot(8.0)
    runs = {c: ContentionMacScheduler(_cfg(c), Rng(3)).schedule(cands, snap, 0) for c in (1, 2, 4)}
    deferred = {c: len(r.deferred) for c, r in runs.items()}
    served = {c: len(r.served) for c, r in runs.items()}
    assert deferred[1] > deferred[2] > deferred[4]  # capture monotonically cuts deferrals
    assert served[4] > served[1]                    # ...and serves more of the same offered load


def test_expired_dropped() -> None:
    sched = ContentionMacScheduler(_BIG, Rng(0))
    result = sched.schedule([_candidate("old", deadline=0)], _snapshot(50.0), step=1)
    assert result.dropped[0].reason is RejectReason.DEADLINE_MISSED


def test_deterministic_for_a_seed() -> None:
    cands = [_candidate(f"u{i}") for i in range(20)]
    a = ContentionMacScheduler(_BIG, Rng(3)).schedule(cands, _snapshot(50.0), step=0)
    b = ContentionMacScheduler(_BIG, Rng(3)).schedule(cands, _snapshot(50.0), step=0)
    assert {r.request_id for r in a.served} == {r.request_id for r in b.served}


def test_contention_runs_via_build_simulation() -> None:
    config = SimulationConfig(seed=1, duration_steps=15, scheduler=SchedulerKind.CONTENTION_MAC)
    sink = InMemoryTelemetrySink()
    summary = build_simulation(config, sink).run()
    assert summary.steps == 15
    assert summary.served > 0
