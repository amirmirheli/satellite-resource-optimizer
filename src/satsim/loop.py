"""Tier-1 control loop — the authoritative per-step scheduling engine.

Wires every port together for one deterministic time step:

1. **Constellation state** — merge per-fleet snapshots (a failing fleet is skipped + counted).
2. **Ingest** — poll the request source and re-admit deferred requests from the retry queue.
3. **Score** — score each request once (single source of truth) under the current Tier-3 plan.
4. **Tier-2 reserve** — the emergency lane claims reserved capacity for urgent traffic; lane-shed
   urgent requests spill into Tier-1 (Tier-2-first, Tier-1-spillover).
5. **Tier-1 admit** — probabilistic load shaping over best-effort + spillover load.
6. **Regulatory filter** — build candidates from each admitted request's legal options.
7. **Schedule** — allocate the capacity left after Tier-2 reservations.
8. **Dispositions** — served / deferred (retry budget + bounded queue) / dropped / rejected.
9. **Telemetry** — per-step counters (and optional per-decision events).
10. **Tier-3 cadence** — periodically re-plan and broadcast the new policy to admission.

Resilience extras (circuit breaker, scheduler fallback chain, safe mode, backoff) layer on in
the next phase; this phase keeps the core loop + retry/deferral lifecycle.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from satsim.config import SimulationConfig
from satsim.domain.enums import (
    URGENT_CLASSES,
    DegradeMode,
    FleetId,
    Region,
    RejectReason,
    SchedulerKind,
    TrafficClass,
)
from satsim.domain.models import (
    Allocation,
    CongestionState,
    ConstellationSnapshot,
    RejectedRequest,
    Satellite,
    ScheduleCandidate,
    SchedulingResult,
    ScoredRequest,
    ServiceRequest,
)
from satsim.domain.planning import PlanningWindow, ResourcePlan
from satsim.domain.telemetry import StepCounters, TelemetryEvent
from satsim.ports.admission import AdmissionController, EmergencyAdmission
from satsim.ports.constellation import ConstellationClient, ConstellationError
from satsim.ports.optimizer import Optimizer
from satsim.ports.regulatory import RegulatoryPolicy
from satsim.ports.request_source import RequestSource
from satsim.ports.scheduler import ResourceScheduler
from satsim.ports.telemetry import TelemetrySink
from satsim.resilience import CircuitBreaker
from satsim.rng import Rng
from satsim.scoring import RequestScorer


@dataclass(slots=True)
class _RetryEntry:
    """A deferred request and the earliest step it may be retried (exponential backoff)."""

    request: ServiceRequest
    eligible_step: int


@dataclass(slots=True)
class RunSummary:
    """Aggregate outcome of a completed run, summed across steps."""

    steps: int = 0
    served: int = 0
    deferred: int = 0
    dropped: int = 0
    rejected: int = 0
    retry_backlog: int = 0  # requests still queued for retry at the end
    fallback_activations: int = 0  # steps that fell back / entered safe mode
    circuit_breaker_trips: int = 0  # constellation breakers newly tripped
    served_by_class: dict[TrafficClass, int] = field(default_factory=dict)


def merge_snapshots(step: int, snapshots: Sequence[ConstellationSnapshot]) -> ConstellationSnapshot:
    """Combine per-fleet snapshots into one merged view for the scheduler."""
    satellites = tuple(sat for snap in snapshots for sat in snap.satellites)
    return ConstellationSnapshot(step=step, satellites=satellites)


def subtract_reservations(
    snapshot: ConstellationSnapshot, reservations: Sequence[Allocation]
) -> ConstellationSnapshot:
    """Return a snapshot with Tier-2 reserved units removed from each beam's capacity."""
    used: dict[str, float] = {}
    for alloc in reservations:
        used[alloc.beam_id] = used.get(alloc.beam_id, 0.0) + alloc.allocated_units
    if not used:
        return snapshot

    satellites: list[Satellite] = []
    for sat in snapshot.satellites:
        beams = tuple(
            replace(b, capacity_units=max(0.0, b.capacity_units - used.get(b.beam_id, 0.0)))
            for b in sat.beams
        )
        satellites.append(replace(sat, beams=beams))
    return ConstellationSnapshot(step=snapshot.step, satellites=tuple(satellites))


# Reasons that mean "urgent request couldn't be reserved but is still viable" -> spill to Tier-1.
_SPILLOVER = RejectReason.EMERGENCY_LANE_FULL


class ControlLoop:
    """Deterministic per-step control loop wiring all ports together."""

    def __init__(
        self,
        config: SimulationConfig,
        *,
        request_source: RequestSource,
        constellations: Sequence[ConstellationClient],
        regulatory: RegulatoryPolicy,
        emergency: EmergencyAdmission,
        admission: AdmissionController,
        scheduler: ResourceScheduler,
        optimizer: Optimizer,
        scorer: RequestScorer,
        telemetry: TelemetrySink,
        rng: Rng,
        fallback_scheduler: ResourceScheduler,
        emit_events: bool = False,
    ) -> None:
        self._config = config
        self._source = request_source
        self._constellations = list(constellations)
        self._regulatory = regulatory
        self._emergency = emergency
        self._admission = admission
        self._scheduler = scheduler
        self._fallback_scheduler = fallback_scheduler
        self._optimizer = optimizer
        self._scorer = scorer
        self._telemetry = telemetry
        self._rng = rng
        self._emit_events = emit_events

        # One circuit breaker per constellation for fail-over to the other fleet.
        self._breakers = {
            client.fleet_id(): CircuitBreaker(
                failure_threshold=config.overload.breaker_failure_threshold,
                cooldown_steps=config.overload.breaker_cooldown_steps,
            )
            for client in self._constellations
        }

        self._plan: ResourcePlan = ResourcePlan.passthrough()
        self._retry_queue: deque[_RetryEntry] = deque()
        self._recent_congestion: deque[CongestionState] = deque(
            maxlen=config.overload.history_capacity
        )
        self._recent_counters: deque[StepCounters] = deque(maxlen=config.overload.history_capacity)
        self._recent_demand: deque[dict[tuple[Region, TrafficClass], float]] = deque(
            maxlen=config.overload.history_capacity
        )
        self._last_drop_rate = 0.0

    # ------------------------------------------------------------------ public

    def run(self) -> RunSummary:
        """Execute the whole simulation and return an aggregate summary."""
        summary = RunSummary()
        for step in range(self._config.duration_steps):
            counters = self.step(step)
            summary.steps += 1
            summary.served += counters.served
            summary.deferred += counters.deferred
            summary.dropped += counters.dropped
            summary.rejected += counters.rejected
            summary.fallback_activations += counters.fallback_activations
            summary.circuit_breaker_trips += counters.circuit_breaker_trips
            for tc, n in counters.served_by_class.items():
                summary.served_by_class[tc] = summary.served_by_class.get(tc, 0) + n
        summary.retry_backlog = len(self._retry_queue)
        return summary

    def step(self, step: int) -> StepCounters:
        """Run one time step; emit and return its aggregate counters."""
        snapshot, breaker_trips = self._gather_constellation(step)
        self._maybe_replan(step, snapshot)

        arrivals = self._ingest(step)
        scored = [self._score(req, step) for req in arrivals]

        # Tier 2: emergency lane over urgent traffic.
        urgent = [s.request for s in scored if s.request.traffic_class in URGENT_CLASSES]
        decision = self._emergency.reserve(urgent, snapshot, self._plan, step)

        remaining = subtract_reservations(snapshot, decision.reservations)

        # Best-effort load = non-urgent + urgent that spilled out of the lane.
        spillover_ids = {r.request.request_id for r in decision.shed if r.reason is _SPILLOVER}
        best_effort = [
            s
            for s in scored
            if s.request.traffic_class not in URGENT_CLASSES
            or s.request.request_id in spillover_ids
        ]

        congestion = self._congestion(step, best_effort, remaining)
        admitted = self._admission.admit(best_effort, congestion, step)

        candidates, no_legal = self._build_candidates(admitted.admitted)
        result, mode, fallback_used = self._run_scheduler(candidates, remaining, step)

        # Assemble dispositions.
        served = list(decision.admitted) + list(result.served)
        allocations = list(decision.reservations) + list(result.allocations)
        deferred_count, requeue_drops = self._handle_deferred(step, result.deferred)
        emergency_drops = [r for r in decision.shed if r.reason is not _SPILLOVER]
        dropped = list(result.dropped) + emergency_drops + requeue_drops
        rejected = list(admitted.rejected) + no_legal

        counters = self._build_counters(
            step=step,
            congestion=congestion,
            snapshot=snapshot,
            served=served,
            allocations=allocations,
            deferred_count=deferred_count,
            dropped=dropped,
            rejected=rejected,
            admitted_count=len(admitted.admitted) + len(decision.admitted),
            fairness_index=result.fairness_index,
            degrade_mode=mode,
            fallback_activations=fallback_used,
            circuit_breaker_trips=breaker_trips,
        )
        self._observe(step, scored, congestion, counters)
        self._telemetry.record_step(counters)
        return counters

    # ------------------------------------------------------------------ stages

    def _gather_constellation(self, step: int) -> tuple[ConstellationSnapshot, int]:
        """Merge per-fleet snapshots, skipping fleets the circuit breaker holds open.

        Returns the merged snapshot and the number of breakers that newly tripped this step.
        """
        snapshots: list[ConstellationSnapshot] = []
        trips = 0
        for client in self._constellations:
            breaker = self._breakers[client.fleet_id()]
            if not breaker.allow(step):
                continue  # open: fail over to the other fleet without attempting
            try:
                snapshots.append(client.visible_resources(step))
                breaker.record_success()
            except ConstellationError:
                if breaker.record_failure(step):
                    trips += 1
                self._emit(
                    TelemetryEvent(step=step, kind="constellation_error", fleet=client.fleet_id())
                )
        return merge_snapshots(step, snapshots), trips

    def _run_scheduler(
        self, candidates: Sequence[ScheduleCandidate], snapshot: ConstellationSnapshot, step: int
    ) -> tuple[SchedulingResult, DegradeMode, int]:
        """Primary scheduler -> fallback heuristic -> safe mode (serve only life-safety).

        Returns the result, the degrade mode in effect, and 1 if a fallback/safe path was used.
        """
        try:
            return self._scheduler.schedule(candidates, snapshot, step), DegradeMode.NORMAL, 0
        except Exception:  # noqa: BLE001 - a scheduler fault must degrade, never crash the loop
            self._emit(TelemetryEvent(step=step, kind="scheduler_fallback"))
        try:
            result = self._fallback_scheduler.schedule(candidates, snapshot, step)
            return result, DegradeMode.FALLBACK, 1
        except Exception:  # noqa: BLE001 - last resort: safe mode
            self._emit(TelemetryEvent(step=step, kind="safe_mode"))
            # Defer all best-effort work; Tier-2 reservations (life-safety) already stand.
            safe = SchedulingResult(deferred=tuple(c.request for c in candidates))
            return safe, DegradeMode.SAFE, 1

    def _ingest(self, step: int) -> list[ServiceRequest]:
        """Poll new arrivals + re-admit deferred requests whose backoff has elapsed."""
        eligible: list[ServiceRequest] = []
        held: deque[_RetryEntry] = deque()
        for entry in self._retry_queue:
            if entry.eligible_step <= step:
                eligible.append(entry.request)
            else:
                held.append(entry)
        self._retry_queue = held
        polled = self._source.poll(step, self._config.arrival.max_batch_per_step)
        return polled + eligible

    def _score(self, request: ServiceRequest, step: int) -> ScoredRequest:
        scoring = self._scorer.evaluate(request, step, self._plan)
        return ScoredRequest(request=request, scoring=scoring)

    def _congestion(
        self,
        step: int,
        best_effort: Sequence[ScoredRequest],
        remaining: ConstellationSnapshot,
    ) -> CongestionState:
        offered = sum(s.scoring.estimated_cost_units for s in best_effort)
        return CongestionState(
            step=step,
            queue_depth=len(self._retry_queue) + self._source_backlog(),
            offered_load_units=offered,
            available_capacity_units=remaining.total_capacity(),
            drop_rate=self._last_drop_rate,
        )

    def _source_backlog(self) -> int:
        """Best-effort read of source-side consumer lag for adapters that expose it."""
        value = getattr(self._source, "backlog", 0)
        if isinstance(value, int):
            return max(0, value)
        return 0

    def _build_candidates(
        self, admitted: Sequence[ScoredRequest]
    ) -> tuple[list[ScheduleCandidate], list[RejectedRequest]]:
        candidates: list[ScheduleCandidate] = []
        no_legal: list[RejectedRequest] = []
        for item in admitted:
            options = tuple(self._regulatory.allowed_options(item.request))
            if not options:
                no_legal.append(RejectedRequest(item.request, RejectReason.NO_LEGAL_OPTION))
                continue
            candidates.append(
                ScheduleCandidate(
                    request=item.request,
                    options=options,
                    scoring=item.scoring,
                    degradable=self._is_degradable(item.request.traffic_class),
                )
            )
        return candidates, no_legal

    def _is_degradable(self, traffic_class: TrafficClass) -> bool:
        profile = self._config.class_mix.get(traffic_class)
        return profile.degradable if profile is not None else False

    def _handle_deferred(
        self, step: int, deferred: Sequence[ServiceRequest]
    ) -> tuple[int, list[RejectedRequest]]:
        """Re-queue deferred requests within retry budget + bounded queue; drop the rest."""
        requeued = 0
        drops: list[RejectedRequest] = []
        capacity = self._config.overload.queue_capacity
        for request in deferred:
            budget = self._retry_budget(request.traffic_class)
            if request.retry_count >= budget:
                drops.append(RejectedRequest(request, RejectReason.RETRY_BUDGET_EXHAUSTED))
            elif len(self._retry_queue) >= capacity:
                drops.append(RejectedRequest(request, RejectReason.NO_CAPACITY))
            else:
                requeued_request = replace(request, retry_count=request.retry_count + 1)
                eligible = step + self._backoff_steps(request.retry_count)
                self._retry_queue.append(_RetryEntry(requeued_request, eligible))
                requeued += 1
        return requeued, drops

    def _backoff_steps(self, retry_count: int) -> int:
        """Exponential backoff (base * 2^retry) capped, plus seeded jitter, to avoid storms."""
        base: int = self._config.overload.retry_backoff_base_steps
        cap: int = self._config.overload.retry_backoff_max_steps
        # 1 << n == 2**n but stays typed as int (int ** int widens to Any in typeshed).
        backoff = min(cap, base * (1 << max(0, retry_count)))
        jitter = self._rng.randint(0, base) if base > 0 else 0
        return backoff + jitter

    def _retry_budget(self, traffic_class: TrafficClass) -> int:
        profile = self._config.class_mix.get(traffic_class)
        return profile.retry_budget if profile is not None else 0

    # ------------------------------------------------------------------ Tier 3

    def _maybe_replan(self, step: int, snapshot: ConstellationSnapshot) -> None:
        if step % self._config.optimizer.cadence_steps != 0:
            return
        by_region = self._aggregate_demand_by_region()
        by_class: dict[TrafficClass, float] = {}
        for (_region, tc), units in by_region.items():
            by_class[tc] = by_class.get(tc, 0.0) + units
        window = PlanningWindow(
            step=step,
            snapshot=snapshot,
            recent_congestion=tuple(self._recent_congestion),
            recent_demand_units=by_class,
            recent_demand_by_region=by_region,
            recent_counters=tuple(self._recent_counters),
        )
        self._plan = self._optimizer.plan(window)
        self._admission.update_policy(self._plan)
        self._emit(TelemetryEvent(step=step, kind="optimizer_run"))

    def _aggregate_demand_by_region(self) -> dict[tuple[Region, TrafficClass], float]:
        total: dict[tuple[Region, TrafficClass], float] = {}
        for snapshot in self._recent_demand:
            for key, units in snapshot.items():
                total[key] = total.get(key, 0.0) + units
        return total

    # ------------------------------------------------------------------ telemetry

    def _build_counters(
        self,
        *,
        step: int,
        congestion: CongestionState,
        snapshot: ConstellationSnapshot,
        served: Sequence[ServiceRequest],
        allocations: Sequence[Allocation],
        deferred_count: int,
        dropped: Sequence[RejectedRequest],
        rejected: Sequence[RejectedRequest],
        admitted_count: int,
        fairness_index: float,
        degrade_mode: DegradeMode,
        fallback_activations: int,
        circuit_breaker_trips: int,
    ) -> StepCounters:
        served_by_class: dict[TrafficClass, int] = {}
        served_by_region: dict[Region, int] = {}
        for request in served:
            served_by_class[request.traffic_class] = (
                served_by_class.get(request.traffic_class, 0) + 1
            )
            served_by_region[request.region] = served_by_region.get(request.region, 0) + 1

        served_by_fleet: dict[FleetId, int] = {}
        allocated_units = 0.0
        degraded = 0
        for alloc in allocations:
            served_by_fleet[alloc.fleet] = served_by_fleet.get(alloc.fleet, 0) + 1
            allocated_units += alloc.allocated_units
            if alloc.degraded:
                degraded += 1

        rejected_by_reason: dict[RejectReason, int] = {}
        for item in (*dropped, *rejected):
            rejected_by_reason[item.reason] = rejected_by_reason.get(item.reason, 0) + 1

        total_capacity = snapshot.total_capacity()
        utilization = allocated_units / total_capacity if total_capacity > 0.0 else 0.0

        return StepCounters(
            step=step,
            admitted=admitted_count,
            rejected=len(rejected),
            served=len(served),
            deferred=deferred_count,
            dropped=len(dropped),
            degraded=degraded,
            served_by_class=served_by_class,
            served_by_region=served_by_region,
            served_by_fleet=served_by_fleet,
            rejected_by_reason=rejected_by_reason,
            fairness_index=fairness_index,
            utilization=min(1.0, utilization),
            collapse_risk=congestion.collapse_risk,
            queue_depth=congestion.queue_depth,
            degrade_mode=degrade_mode,
            fallback_activations=fallback_activations,
            circuit_breaker_trips=circuit_breaker_trips,
        )

    def _observe(
        self,
        step: int,
        scored: Sequence[ScoredRequest],
        congestion: CongestionState,
        counters: StepCounters,
    ) -> None:
        # Record offered demand (airtime cost) per (region, class) + congestion/counters for Tier 3.
        demand: dict[tuple[Region, TrafficClass], float] = {}
        for item in scored:
            key = (item.request.region, item.request.traffic_class)
            demand[key] = demand.get(key, 0.0) + item.scoring.estimated_cost_units
        self._recent_demand.append(demand)
        self._recent_congestion.append(congestion)
        self._recent_counters.append(counters)

        handled = counters.served + counters.dropped + counters.rejected
        self._last_drop_rate = counters.dropped / handled if handled > 0 else 0.0

    def _emit(self, event: TelemetryEvent) -> None:
        if self._emit_events:
            self._telemetry.emit(event)


def build_simulation(
    config: SimulationConfig, telemetry: TelemetrySink, *, emit_events: bool = False
) -> ControlLoop:
    """Composition root: wire the default fakes/adapters into a runnable control loop."""
    # Local imports keep the core module import-light and avoid any import cycles.
    from satsim.adapters.admission import ProbabilisticAdmissionController
    from satsim.adapters.constellation import build_fake_constellations
    from satsim.adapters.emergency import EmergencyLane
    from satsim.adapters.optimizer import build_optimizer
    from satsim.adapters.regulatory import TableRegulatoryPolicy
    from satsim.adapters.request_source import SyntheticRequestSource
    from satsim.adapters.scheduler import HeuristicScheduler

    rng = Rng(config.seed)
    regulatory = TableRegulatoryPolicy(config)
    return ControlLoop(
        config,
        request_source=SyntheticRequestSource(config, rng.derive("demand")),
        constellations=build_fake_constellations(config),
        regulatory=regulatory,
        emergency=EmergencyLane(config.emergency, regulatory, config.scoring),
        admission=ProbabilisticAdmissionController(rng.derive("admission")),
        scheduler=_build_scheduler(config),
        # Fallback chain: a plain heuristic scheduler is the simplest robust allocator.
        fallback_scheduler=HeuristicScheduler(config.scheduler_params.degrade_min_fraction),
        optimizer=build_optimizer(config),
        scorer=RequestScorer(config),
        telemetry=telemetry,
        rng=rng.derive("loop"),
        emit_events=emit_events,
    )


def _build_scheduler(config: SimulationConfig) -> ResourceScheduler:
    """Select the primary scheduler from ``config.scheduler``."""
    from satsim.adapters.mac import SlotMacScheduler
    from satsim.adapters.scheduler import HeuristicScheduler, PriorityFairScheduler

    sched = config.scheduler_params
    degrade = sched.degrade_min_fraction
    if config.scheduler is SchedulerKind.SLOT_MAC:
        return SlotMacScheduler(config.mac)
    if config.scheduler is SchedulerKind.PRIORITY_FAIR:
        return PriorityFairScheduler(sched.priority_fair_max_units_per_request, degrade)
    return HeuristicScheduler(degrade)
