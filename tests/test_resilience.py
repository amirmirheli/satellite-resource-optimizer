"""Tests for resilience: circuit breaker, fleet failover, scheduler fallback chain, backoff."""

from __future__ import annotations

from collections.abc import Sequence

from satsim.adapters.admission import ProbabilisticAdmissionController
from satsim.adapters.constellation import build_fake_constellations
from satsim.adapters.emergency import EmergencyLane
from satsim.adapters.optimizer import build_optimizer
from satsim.adapters.regulatory import TableRegulatoryPolicy
from satsim.adapters.request_source import SyntheticRequestSource
from satsim.adapters.scheduler import HeuristicScheduler
from satsim.adapters.telemetry import InMemoryTelemetrySink
from satsim.config import OverloadConfig, SimulationConfig
from satsim.domain.enums import DegradeMode, FleetId
from satsim.domain.models import ConstellationSnapshot, ScheduleCandidate, SchedulingResult
from satsim.loop import ControlLoop
from satsim.ports.constellation import ConstellationClient, ConstellationError
from satsim.ports.scheduler import ResourceScheduler
from satsim.resilience import CircuitBreaker
from satsim.rng import Rng
from satsim.scoring import RequestScorer

# --------------------------------------------------------------------------- circuit breaker


def test_breaker_trips_after_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=3, cooldown_steps=5)
    assert cb.allow(0)
    assert cb.record_failure(0) is False
    assert cb.record_failure(0) is False
    assert cb.record_failure(0) is True  # third failure trips it
    assert cb.is_open
    assert not cb.allow(1)  # open during cooldown


def test_breaker_half_open_then_recovers() -> None:
    cb = CircuitBreaker(failure_threshold=1, cooldown_steps=5)
    cb.record_failure(0)  # trips, open until step 5
    assert not cb.allow(4)
    assert cb.allow(5)  # half-open trial allowed after cooldown
    cb.record_success()
    assert not cb.is_open
    assert cb.allow(6)


def test_breaker_half_open_failure_reopens() -> None:
    cb = CircuitBreaker(failure_threshold=1, cooldown_steps=5)
    cb.record_failure(0)
    assert cb.allow(5)  # half-open
    assert cb.record_failure(5) is False  # trial failed, not a *new* trip
    assert not cb.allow(6)  # reopened until step 10
    assert cb.allow(10)


# --------------------------------------------------------------------------- loop wiring helpers


class _FailingClient:
    """Wraps a real constellation client but always fails its resource lookup."""

    def __init__(self, inner: ConstellationClient) -> None:
        self._inner = inner

    def fleet_id(self) -> FleetId:
        return self._inner.fleet_id()

    def visible_resources(self, step: int) -> ConstellationSnapshot:
        raise ConstellationError("injected fault")


class _BoomScheduler:
    """Scheduler that always raises (to exercise the fallback chain)."""

    def schedule(
        self, candidates: Sequence[ScheduleCandidate], snapshot: ConstellationSnapshot, step: int
    ) -> SchedulingResult:
        raise RuntimeError("scheduler boom")


def _build_loop(
    config: SimulationConfig,
    sink: InMemoryTelemetrySink,
    *,
    constellations: Sequence[ConstellationClient] | None = None,
    scheduler: ResourceScheduler | None = None,
    fallback: ResourceScheduler | None = None,
) -> ControlLoop:
    rng = Rng(config.seed)
    regulatory = TableRegulatoryPolicy(config)
    return ControlLoop(
        config,
        request_source=SyntheticRequestSource(config, rng.derive("demand")),
        constellations=constellations or build_fake_constellations(config),
        regulatory=regulatory,
        emergency=EmergencyLane(config.emergency, regulatory),
        admission=ProbabilisticAdmissionController(rng.derive("admission")),
        scheduler=scheduler or HeuristicScheduler(),
        fallback_scheduler=fallback or HeuristicScheduler(),
        optimizer=build_optimizer(config),
        scorer=RequestScorer(config),
        telemetry=sink,
        rng=rng.derive("loop"),
    )


# --------------------------------------------------------------------------- failover


def test_failing_fleet_trips_breaker_but_other_fleet_serves() -> None:
    # Legacy LEO is online from step 0; fail NEXT_GEN so only the legacy fleet is usable.
    config = SimulationConfig(seed=1, duration_steps=20)
    clients = list(build_fake_constellations(config))
    wrapped = [
        _FailingClient(c) if c.fleet_id() is FleetId.NEXT_GEN else c for c in clients
    ]
    sink = InMemoryTelemetrySink()
    loop = _build_loop(config, sink, constellations=wrapped)
    summary = loop.run()

    assert summary.circuit_breaker_trips > 0  # the failing fleet tripped its breaker
    assert summary.served > 0  # the healthy fleet kept serving (failover)


# --------------------------------------------------------------------------- scheduler fallback


def test_scheduler_fallback_engages() -> None:
    config = SimulationConfig(seed=2, duration_steps=10)
    sink = InMemoryTelemetrySink()
    loop = _build_loop(config, sink, scheduler=_BoomScheduler(), fallback=HeuristicScheduler())
    summary = loop.run()
    assert summary.fallback_activations > 0
    assert any(s.degrade_mode is DegradeMode.FALLBACK for s in sink.steps)
    assert summary.served > 0  # fallback still serves


def test_safe_mode_when_both_schedulers_fail() -> None:
    config = SimulationConfig(seed=3, duration_steps=8)
    sink = InMemoryTelemetrySink()
    loop = _build_loop(config, sink, scheduler=_BoomScheduler(), fallback=_BoomScheduler())
    summary = loop.run()  # must not crash
    assert any(s.degrade_mode is DegradeMode.SAFE for s in sink.steps)
    # Best-effort is all deferred in safe mode; nothing best-effort is served.
    assert summary.steps == 8


# --------------------------------------------------------------------------- backoff


def test_backoff_grows_and_is_capped() -> None:
    config = SimulationConfig(
        seed=4, overload=OverloadConfig(queue_capacity=100, retry_backoff_base_steps=1,
                                        retry_backoff_max_steps=16)
    )
    loop = _build_loop(config, InMemoryTelemetrySink())
    first = loop._backoff_steps(0)
    later = loop._backoff_steps(5)
    assert later > first
    assert later <= 16 + 1  # capped at max + at most `base` jitter
