"""Tests for the SlotMacScheduler (resource-block grid + MCS link adaptation)."""

from __future__ import annotations

from satsim.adapters.mac import SlotMacScheduler
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

_FLEET = FleetId.NEXT_GEN
_BAND = Band.KU
_REGION = Region.NA


def _snapshot(capacity: float, *, beams: int = 1) -> ConstellationSnapshot:
    made = tuple(
        Beam(f"b{i}", _FLEET, "s", _REGION, _BAND, capacity) for i in range(beams)
    )
    return ConstellationSnapshot(step=0, satellites=(Satellite("s", _FLEET, made),))


def _candidate(
    rid: str, *, score: float, size: int, link: float, degradable: bool = False, deadline: int = 10
) -> ScheduleCandidate:
    request = ServiceRequest(
        request_id=rid, traffic_class=TrafficClass.MESSAGING, region=_REGION, size_bytes=size,
        link_quality=link, urgency=0.0, arrival_step=0, deadline_step=deadline,
    )
    return ScheduleCandidate(
        request=request,
        options=(CandidateOption(fleet=_FLEET, band=_BAND),),
        scoring=RequestScore(score=score, estimated_cost_units=1.0, delivery_probability=link),
        degradable=degradable,
    )


# Small grid: 2 RBs/beam, 1000 symbols/RB. At 256QAM (se=8) one RB carries 8000 bits = 1000 bytes.
_SMALL = MacConfig(slots_per_step=2, subchannels=1, symbols_per_rb=1000)
# Large grid for link-adaptation comparisons: 10 RBs/beam.
_LARGE = MacConfig(slots_per_step=10, subchannels=1, symbols_per_rb=1000)


def test_satisfies_port() -> None:
    assert isinstance(SlotMacScheduler(_SMALL), ResourceScheduler)


def test_link_adaptation_poor_link_uses_more_rbs() -> None:
    # Same 1000-byte payload: good link needs 1 RB, poor link (QPSK) needs 4 -> more airtime.
    sched = SlotMacScheduler(_LARGE)
    snap = _snapshot(10.0)  # 10 RBs, rb_units = 1.0
    good = sched.schedule([_candidate("g", score=0.5, size=1000, link=0.9)], snap, step=0)
    poor = sched.schedule([_candidate("p", score=0.5, size=1000, link=0.3)], snap, step=0)
    assert good.allocations[0].allocated_units == 1.0   # 1 RB at 256QAM
    assert poor.allocations[0].allocated_units == 4.0   # 4 RBs at QPSK


def test_grid_fills_then_defers() -> None:
    # 2-RB beam, three 1-RB (good-link, 1000-byte) requests: two served, one deferred.
    sched = SlotMacScheduler(_SMALL)
    snap = _snapshot(2.0)
    cands = [_candidate(f"r{i}", score=0.9 - i * 0.1, size=1000, link=0.9) for i in range(3)]
    result = sched.schedule(cands, snap, step=0)
    assert len(result.served) == 2
    assert len(result.deferred) == 1


def test_oversized_degradable_served_partial_else_deferred() -> None:
    sched = SlotMacScheduler(_SMALL)
    snap = _snapshot(2.0)  # only 2 RBs
    # 1000-byte payload on a poor link needs 4 RBs > 2 available.
    degradable = sched.schedule(
        [_candidate("photo", score=0.3, size=1000, link=0.3, degradable=True)], snap, step=0
    )
    assert degradable.allocations[0].degraded is True
    assert degradable.allocations[0].allocated_units == 2.0  # got all free RBs

    rigid = sched.schedule(
        [_candidate("msg", score=0.3, size=1000, link=0.3, degradable=False)], snap, step=0
    )
    assert rigid.served == ()
    assert [r.request_id for r in rigid.deferred] == ["msg"]


def test_per_request_rb_cap_limits_allocation() -> None:
    sched = SlotMacScheduler(MacConfig(slots_per_step=10, subchannels=1, symbols_per_rb=1000,
                                       max_rbs_per_request=2))
    snap = _snapshot(10.0)
    # Needs 4 RBs but the cap is 2 and it's not degradable -> can't fully fit -> deferred.
    result = sched.schedule(
        [_candidate("p", score=0.5, size=1000, link=0.3)], snap, step=0
    )
    assert result.deferred and not result.served


def test_expired_dropped() -> None:
    sched = SlotMacScheduler(_SMALL)
    result = sched.schedule(
        [_candidate("old", score=0.9, size=100, link=0.9, deadline=0)], _snapshot(2.0), step=1
    )
    assert result.dropped[0].reason is RejectReason.DEADLINE_MISSED


def test_higher_score_served_first() -> None:
    sched = SlotMacScheduler(_SMALL)  # 2 RBs
    snap = _snapshot(2.0)
    # Three 1-RB requests; only two fit, highest scores win.
    cands = [
        _candidate("low", score=0.1, size=1000, link=0.9),
        _candidate("mid", score=0.5, size=1000, link=0.9),
        _candidate("high", score=0.9, size=1000, link=0.9),
    ]
    served = {r.request_id for r in sched.schedule(cands, snap, step=0).served}
    assert served == {"high", "mid"}


def test_slot_mac_runs_via_build_simulation() -> None:
    config = SimulationConfig(seed=1, duration_steps=15, scheduler=SchedulerKind.SLOT_MAC)
    sink = InMemoryTelemetrySink()
    summary = build_simulation(config, sink).run()
    assert summary.steps == 15
    assert summary.served > 0
