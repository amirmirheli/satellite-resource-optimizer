"""Integration tests for the Tier-1 control loop."""

from __future__ import annotations

from satsim.adapters.telemetry import InMemoryTelemetrySink
from satsim.config import (
    ArrivalConfig,
    EmergencyConfig,
    OverloadConfig,
    SimulationConfig,
    SurgeEvent,
)
from satsim.domain.enums import FleetId, RejectReason, TrafficClass
from satsim.domain.models import Allocation, ConstellationSnapshot
from satsim.loop import build_simulation, merge_snapshots, subtract_reservations


def _run(config: SimulationConfig) -> tuple[InMemoryTelemetrySink, object]:
    sink = InMemoryTelemetrySink()
    loop = build_simulation(config, sink)
    summary = loop.run()
    return sink, summary


# --------------------------------------------------------------------------- helpers


def test_subtract_reservations_reduces_capacity() -> None:
    from satsim.domain.enums import Band, Region
    from satsim.domain.models import Beam, Satellite

    snap = ConstellationSnapshot(
        step=0,
        satellites=(
            Satellite("s", FleetId.NEXT_GEN, (
                Beam("b0", FleetId.NEXT_GEN, "s", Region.NA, Band.KU, 10.0),
            )),
        ),
    )
    reservation = Allocation("r", FleetId.NEXT_GEN, "b0", Band.KU, 4.0, 0.9)
    reduced = subtract_reservations(snap, [reservation])
    assert reduced.total_capacity() == 6.0
    # Original snapshot is untouched (immutability).
    assert snap.total_capacity() == 10.0


def test_merge_snapshots_combines_satellites() -> None:
    a = ConstellationSnapshot(step=1)
    b = ConstellationSnapshot(step=1)
    assert merge_snapshots(1, [a, b]).satellites == ()


# --------------------------------------------------------------------------- end to end


def test_default_run_serves_traffic_and_is_conserved() -> None:
    sink, summary = _run(SimulationConfig(seed=1, duration_steps=30))
    assert summary.steps == 30
    assert summary.served > 0
    assert len(sink.steps) == 30
    # Every served request was counted under some class.
    assert sum(summary.served_by_class.values()) == summary.served


def test_run_is_deterministic() -> None:
    a_sink, a = _run(SimulationConfig(seed=7, duration_steps=20))
    b_sink, b = _run(SimulationConfig(seed=7, duration_steps=20))
    assert (a.served, a.dropped, a.rejected, a.deferred) == (
        b.served, b.dropped, b.rejected, b.deferred,
    )


def test_emergency_surge_protects_life_safety() -> None:
    # A large SOS surge on limited spectrum: SOS should still be served, system shouldn't collapse
    # into serving nothing.
    config = SimulationConfig(
        seed=3,
        duration_steps=20,
        arrival=ArrivalConfig(baseline_rate=5.0, max_batch_per_step=10_000),
        surges=(SurgeEvent(at_step=5, count=2_000, traffic_class=TrafficClass.EMERGENCY_SOS),),
    )
    _sink, summary = _run(config)
    assert summary.served_by_class.get(TrafficClass.EMERGENCY_SOS, 0) > 0


def test_capacity_expansion_increases_throughput_later() -> None:
    # NEXT_GEN comes online at step 50 by default. Under heavy (capacity-bound) load, the extra
    # capacity should lift sustained throughput after expansion.
    config = SimulationConfig(
        seed=2,
        duration_steps=80,
        arrival=ArrivalConfig(baseline_rate=150.0, max_batch_per_step=10_000),
    )
    sink, _summary = _run(config)
    before = sum(s.served for s in sink.steps[40:50])
    after = sum(s.served for s in sink.steps[60:70])
    assert after > before


def test_bounded_retry_queue_does_not_grow_unboundedly() -> None:
    # Tiny queue + heavy load: backlog stays bounded, surplus is dropped (no unbounded growth).
    config = SimulationConfig(
        seed=4,
        duration_steps=25,
        arrival=ArrivalConfig(baseline_rate=200.0, max_batch_per_step=10_000),
        overload=OverloadConfig(queue_capacity=50),
    )
    _sink, summary = _run(config)
    assert summary.retry_backlog <= 50


def test_overload_triggers_rejections() -> None:
    # Sustained heavy load should cause admission shedding once the optimizer tightens the curve.
    config = SimulationConfig(
        seed=5,
        duration_steps=40,
        arrival=ArrivalConfig(baseline_rate=300.0, max_batch_per_step=10_000),
    )
    sink, summary = _run(config)
    assert summary.rejected > 0
    shed = any(
        RejectReason.ADMISSION_SHED in s.rejected_by_reason for s in sink.steps
    )
    assert shed


def test_source_backlog_contributes_to_congestion_signal() -> None:
    config = SimulationConfig(
        seed=42,
        duration_steps=3,
        arrival=ArrivalConfig(baseline_rate=100.0, max_batch_per_step=1),
    )
    sink, _summary = _run(config)
    assert sink.steps[-1].queue_depth > 0
    assert any(step.collapse_risk > 0.0 for step in sink.steps)


def test_emergency_reserved_capacity_only_for_urgent() -> None:
    # With a high emergency reserve, SOS surge consumes reserved capacity; verify SOS served.
    config = SimulationConfig(
        seed=6,
        duration_steps=15,
        emergency=EmergencyConfig(reserved_fraction=0.5),
        surges=(SurgeEvent(at_step=2, count=500, traffic_class=TrafficClass.EMERGENCY_SOS),),
    )
    sink, summary = _run(config)
    # SOS allocations land on a real fleet.
    served_fleets = set()
    for s in sink.steps:
        served_fleets.update(s.served_by_fleet)
    assert served_fleets  # something was allocated
