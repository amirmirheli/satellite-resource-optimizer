"""End-to-end scenario tests — each exercises one story from the brief on fakes."""

from __future__ import annotations

from satsim.adapters.io import InMemoryTelemetrySink
from satsim.domain.enums import RejectReason, TrafficClass
from satsim.runtime.loop import RunSummary, build_simulation
from satsim.scenarios import build_scenario, scenario_names


def _run(name: str, *, steps: int | None = None) -> tuple[InMemoryTelemetrySink, RunSummary]:
    config = build_scenario(name, steps=steps)
    sink = InMemoryTelemetrySink()
    summary = build_simulation(config, sink).run()
    return sink, summary


def test_all_scenarios_run_without_error() -> None:
    for name in scenario_names():
        _sink, summary = _run(name, steps=12)
        assert summary.steps == 12


def test_emergency_surge_protects_life_safety() -> None:
    # The SOS surge fires at step 10; run past it. Life-safety dominates what gets served.
    _sink, summary = _run("emergency_surge", steps=25)
    sos = summary.served_by_class.get(TrafficClass.EMERGENCY_SOS, 0)
    assert sos > 0
    assert sos == max(summary.served_by_class.values())  # SOS is the most-served class


def test_mixed_roadmap_serves_priority_and_exercises_degradation() -> None:
    sink, summary = _run("mixed_roadmap", steps=40)
    # Priority traffic stays reliable.
    assert summary.served_by_class.get(TrafficClass.EMERGENCY_SOS, 0) > 0
    assert summary.served_by_class.get(TrafficClass.ROADSIDE, 0) > 0
    # Heavy best-effort either gets served (possibly degraded) or absorbs backpressure (deferred).
    degraded = sum(s.degraded for s in sink.steps)
    photo_served = summary.served_by_class.get(TrafficClass.PHOTO, 0)
    assert degraded > 0 or photo_served > 0 or summary.deferred > 0


def test_capacity_expansion_lifts_throughput() -> None:
    sink, _summary = _run("capacity_expansion", steps=80)  # next-gen online at 50
    before = sum(s.served for s in sink.steps[40:50])
    after = sum(s.served for s in sink.steps[60:70])
    assert after > before


def test_regulatory_denial_rejects_unlicensed_region() -> None:
    sink, _summary = _run("regulatory_denial", steps=20)
    saw_denial = any(
        RejectReason.NO_LEGAL_OPTION in s.rejected_by_reason for s in sink.steps
    )
    assert saw_denial


def test_constellation_fault_trips_breaker_and_fails_over() -> None:
    _sink, summary = _run("constellation_fault", steps=30)  # next-gen fails from step 15
    assert summary.circuit_breaker_trips > 0
    assert summary.served > 0  # legacy fleet keeps serving


def test_poor_link_scenario_still_serves_life_safety() -> None:
    _sink, summary = _run("poor_link_vs_good_link", steps=20)
    assert summary.served_by_class.get(TrafficClass.EMERGENCY_SOS, 0) > 0


def test_scenarios_are_deterministic() -> None:
    a_sink, a = _run("mixed_roadmap", steps=15)
    b_sink, b = _run("mixed_roadmap", steps=15)
    assert (a.served, a.dropped, a.rejected) == (b.served, b.dropped, b.rejected)
