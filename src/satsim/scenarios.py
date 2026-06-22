"""Named scenarios — runnable :class:`SimulationConfig`s for each story in the brief.

Each scenario is a config the CLI (and the scenario tests) can run end-to-end on fakes. They
exercise: emergency surge, poor-link vs good-link, mixed roadmap load, capacity expansion mid-run,
regulatory denial, and a constellation fault.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from satsim.config import (
    ArrivalConfig,
    EmergencyConfig,
    SimulationConfig,
    SurgeEvent,
)
from satsim.domain.enums import FleetId, Region, SchedulerKind, TrafficClass


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named, documented scenario plus its config builder."""

    name: str
    description: str
    build: Callable[[], SimulationConfig]


def _emergency_surge() -> SimulationConfig:
    """Thousands of SOS arrive at once on limited spectrum; life-safety must stay reliable."""
    return SimulationConfig(
        seed=1,
        duration_steps=60,
        arrival=ArrivalConfig(baseline_rate=10.0, max_batch_per_step=20_000),
        surges=(SurgeEvent(at_step=10, count=3_000, traffic_class=TrafficClass.EMERGENCY_SOS),),
        emergency=EmergencyConfig(reserved_fraction=0.25),
    )


def _poor_link_vs_good_link() -> SimulationConfig:
    """A poor-link environment: bounded opportunity + airtime caps keep beams from being hogged."""
    return SimulationConfig(
        seed=2,
        duration_steps=40,
        arrival=ArrivalConfig(baseline_rate=50.0, max_batch_per_step=20_000, link_quality_max=0.25),
        scheduler=SchedulerKind.PRIORITY_FAIR,  # per-request airtime cap matters here
        surges=(SurgeEvent(at_step=5, count=300, traffic_class=TrafficClass.EMERGENCY_SOS),),
    )


def _mixed_roadmap() -> SimulationConfig:
    """All classes competing: heavy best-effort absorbs backpressure; priority stays reliable."""
    return SimulationConfig(
        seed=3,
        duration_steps=60,
        arrival=ArrivalConfig(baseline_rate=60.0, max_batch_per_step=20_000),
    )


def _capacity_expansion() -> SimulationConfig:
    """Next-gen fleet comes online at step 50; sustained throughput should rise afterwards."""
    return SimulationConfig(
        seed=4,
        duration_steps=120,
        arrival=ArrivalConfig(baseline_rate=150.0, max_batch_per_step=20_000),
    )


def _regulatory_denial() -> SimulationConfig:
    """One region is licensed to no constellation: its requests are rejected (no legal option)."""
    base = SimulationConfig()
    rules = tuple(
        rule.model_copy(update={"allowed_fleets": ()}) if rule.region is Region.OCEAN else rule
        for rule in base.region_rules
    )
    return SimulationConfig(
        seed=5,
        duration_steps=40,
        arrival=ArrivalConfig(baseline_rate=40.0, max_batch_per_step=20_000),
        region_rules=rules,
    )


def _constellation_fault() -> SimulationConfig:
    """Next-gen starts failing mid-run: the breaker trips and traffic fails over to legacy."""
    base = SimulationConfig()
    fleets = tuple(
        fleet.model_copy(update={"online_at_step": 0, "fail_from_step": 15})
        if fleet.fleet is FleetId.NEXT_GEN
        else fleet
        for fleet in base.fleets
    )
    return SimulationConfig(
        seed=6,
        duration_steps=40,
        arrival=ArrivalConfig(baseline_rate=60.0, max_batch_per_step=20_000),
        fleets=fleets,
    )


_BUILDERS: tuple[tuple[str, Callable[[], SimulationConfig]], ...] = (
    ("emergency_surge", _emergency_surge),
    ("poor_link_vs_good_link", _poor_link_vs_good_link),
    ("mixed_roadmap", _mixed_roadmap),
    ("capacity_expansion", _capacity_expansion),
    ("regulatory_denial", _regulatory_denial),
    ("constellation_fault", _constellation_fault),
)

SCENARIOS: dict[str, Scenario] = {
    name: Scenario(name, (build.__doc__ or "").strip(), build) for name, build in _BUILDERS
}


def scenario_names() -> tuple[str, ...]:
    """All registered scenario names, in registration order."""
    return tuple(SCENARIOS)


def build_scenario(
    name: str, *, seed: int | None = None, steps: int | None = None
) -> SimulationConfig:
    """Build a scenario's config, optionally overriding the seed and/or duration."""
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario: {name!r}")
    config = SCENARIOS[name].build()
    overrides: dict[str, object] = {}
    if seed is not None:
        overrides["seed"] = seed
    if steps is not None:
        overrides["duration_steps"] = steps
        overrides["surges"] = tuple(surge for surge in config.surges if surge.at_step < steps)
    if not overrides:
        return config
    data = config.model_dump()
    data.update(overrides)
    return SimulationConfig.model_validate(data)
