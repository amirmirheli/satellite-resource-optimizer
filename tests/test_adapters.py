"""Tests for the license-free fakes (phase-1 adapters)."""

from __future__ import annotations

import pytest

from satsim.adapters.constellation import FakeConstellation, build_fake_constellations
from satsim.adapters.regulatory import TableRegulatoryPolicy
from satsim.adapters.request_source import SyntheticRequestSource
from satsim.adapters.telemetry import ConsoleTelemetrySink, InMemoryTelemetrySink
from satsim.bus import InMemoryBus
from satsim.config import SimulationConfig
from satsim.domain.enums import Band, FleetId, Outcome, Region, TrafficClass
from satsim.domain.models import ServiceRequest
from satsim.domain.telemetry import StepCounters, TelemetryEvent
from satsim.ports.constellation import ConstellationClient, ConstellationError
from satsim.ports.regulatory import RegulatoryPolicy
from satsim.ports.request_source import RequestSource
from satsim.ports.telemetry import TelemetrySink
from satsim.rng import Rng


def _request(region: Region = Region.NA, band: Band | None = None) -> ServiceRequest:
    return ServiceRequest(
        request_id="r", traffic_class=TrafficClass.MESSAGING, region=region,
        size_bytes=128, link_quality=0.9, urgency=0.2, arrival_step=0, deadline_step=5,
        preferred_band=band,
    )


# --------------------------------------------------------------------------- ports


def test_fakes_satisfy_their_ports(default_config: SimulationConfig) -> None:
    rng = Rng(0)
    assert isinstance(SyntheticRequestSource(default_config, rng), RequestSource)
    assert isinstance(build_fake_constellations(default_config)[0], ConstellationClient)
    assert isinstance(TableRegulatoryPolicy(default_config), RegulatoryPolicy)
    assert isinstance(InMemoryTelemetrySink(), TelemetrySink)
    assert isinstance(ConsoleTelemetrySink(), TelemetrySink)


# --------------------------------------------------------------------------- bus


def test_bus_respects_availability_and_batch() -> None:
    bus = InMemoryBus()
    bus.publish(_request(), available_at_step=0)
    bus.publish(_request(), available_at_step=2)
    # Only the step-0 request is available at step 0.
    assert len(bus.drain(0, 10)) == 1
    assert len(bus) == 1
    # Still not available at step 1.
    assert bus.drain(1, 10) == []
    # Available at step 2.
    assert len(bus.drain(2, 10)) == 1
    assert len(bus) == 0


def test_bus_batch_cap_preserves_backlog() -> None:
    bus = InMemoryBus()
    for _ in range(5):
        bus.publish(_request(), available_at_step=0)
    assert len(bus.drain(0, 2)) == 2
    assert len(bus) == 3


# --------------------------------------------------------------------------- demand


def test_request_source_is_deterministic() -> None:
    cfg = SimulationConfig(seed=123)
    a = SyntheticRequestSource(cfg, Rng(cfg.seed))
    b = SyntheticRequestSource(cfg, Rng(cfg.seed))
    ra = [r.request_id for r in a.poll(0, 10_000)]
    rb = [r.request_id for r in b.poll(0, 10_000)]
    assert ra == rb
    assert ra  # produced something with the default baseline rate


def test_surge_injects_requests_at_step() -> None:
    from satsim.config import ArrivalConfig, SurgeEvent

    cfg = SimulationConfig(
        seed=1,
        arrival=ArrivalConfig(baseline_rate=0.0, max_batch_per_step=10_000),
        surges=(SurgeEvent(at_step=3, count=50, traffic_class=TrafficClass.EMERGENCY_SOS),),
    )
    src = SyntheticRequestSource(cfg, Rng(cfg.seed))
    assert src.poll(0, 10_000) == []  # no baseline, no surge yet
    surged = src.poll(3, 10_000)
    assert len(surged) == 50
    assert all(r.traffic_class is TrafficClass.EMERGENCY_SOS for r in surged)


def test_backlog_grows_when_undrained() -> None:
    cfg = SimulationConfig(seed=7)
    src = SyntheticRequestSource(cfg, Rng(cfg.seed))
    src.poll(0, 1)  # drain just one; rest is backlog
    assert src.backlog > 0


# --------------------------------------------------------------------------- constellation


def test_constellation_online_gating_and_capacity() -> None:
    cfg = SimulationConfig()
    by_fleet = {c.fleet_id(): c for c in build_fake_constellations(cfg)}
    nextgen = by_fleet[FleetId.NEXT_GEN]  # online_at_step=50 by default
    assert nextgen.visible_resources(0).total_capacity() == 0.0  # not online yet
    assert nextgen.visible_resources(50).total_capacity() > 0.0  # capacity expansion


def test_constellation_coverage_is_licensed_only() -> None:
    cfg = SimulationConfig()
    by_fleet = {c.fleet_id(): c for c in build_fake_constellations(cfg)}
    # OCEAN permits only NEXT_GEN; LEGACY_LEO must never illuminate OCEAN.
    legacy = by_fleet[FleetId.LEGACY_LEO]
    regions = {beam.region for beam in legacy.visible_resources(0).beams}
    assert Region.OCEAN not in regions


def test_constellation_fault_raises() -> None:
    fake = FakeConstellation(
        fleet=FleetId.LEGACY_LEO, num_satellites=1, beams_per_satellite=1,
        capacity_per_beam=1.0, coverage=((Region.NA, Band.L),), fail_from_step=5,
    )
    assert fake.visible_resources(4).total_capacity() == 1.0
    with pytest.raises(ConstellationError):
        fake.visible_resources(5)


# --------------------------------------------------------------------------- regulatory


def test_regulatory_allows_licensed_and_denies_unlicensed() -> None:
    reg = TableRegulatoryPolicy(SimulationConfig())
    # APAC permits only LEGACY_LEO on band S (per defaults).
    assert reg.evaluate(Region.APAC, FleetId.LEGACY_LEO, Band.S).allowed
    assert not reg.evaluate(Region.APAC, FleetId.NEXT_GEN, Band.S).allowed
    assert not reg.evaluate(Region.APAC, FleetId.LEGACY_LEO, Band.KA).allowed


def test_regulatory_options_and_preferred_band_first() -> None:
    reg = TableRegulatoryPolicy(SimulationConfig())
    options = reg.allowed_options(_request(region=Region.NA, band=Band.KU))
    assert options
    assert options[0].band is Band.KU  # preferred band surfaced first
    # APAC carries an airtime cap in the defaults.
    apac = reg.allowed_options(_request(region=Region.APAC))
    assert all(o.constraints.max_airtime_units == 8.0 for o in apac)


def test_regulatory_unknown_region_has_no_options() -> None:
    from satsim.config import RegionRule

    # A config whose rules cover only NA: a request in EU gets no legal options.
    cfg = SimulationConfig(
        region_rules=(
            RegionRule(
                region=Region.NA, allowed_bands=(Band.L,), allowed_fleets=(FleetId.LEGACY_LEO,)
            ),
        )
    )
    reg = TableRegulatoryPolicy(cfg)
    assert reg.allowed_options(_request(region=Region.EU)) == []
    assert not reg.evaluate(Region.EU, FleetId.LEGACY_LEO, Band.L).allowed


# --------------------------------------------------------------------------- telemetry


def test_in_memory_sink_records_and_filters() -> None:
    sink = InMemoryTelemetrySink()
    sink.emit(TelemetryEvent(step=0, kind="admit"))
    sink.emit(TelemetryEvent(step=0, kind="reject"))
    sink.record_step(StepCounters(step=0, served=3))
    assert len(sink.events_of_kind("admit")) == 1
    assert sink.total_served() == 3
    assert sink.last_step is not None and sink.last_step.step == 0


def test_console_sink_prints_step(capsys) -> None:  # type: ignore[no-untyped-def]
    sink = ConsoleTelemetrySink(emit_events=True)
    sink.emit(
        TelemetryEvent(
            step=1, kind="served", traffic_class=TrafficClass.EMERGENCY_SOS,
            region=Region.NA, fleet=FleetId.NEXT_GEN, detail={"units": 2},
        )
    )
    sink.record_step(StepCounters(step=1, served=2, utilization=0.5))
    out = capsys.readouterr().out
    assert "kind=served" in out
    assert "step    1" in out
    assert Outcome.SERVED.value in out  # "served" appears in both lines
