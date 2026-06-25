"""Default :class:`~satsim.ports.scheduler.ResourceScheduler` implementations.

Both schedulers share a greedy, highest-score-first allocation core (:class:`_GreedyScheduler`)
and differ only in their fairness constraint:

* :class:`HeuristicScheduler` — pure priority + weighted-fair: no per-request cap, so a single
  high-score request may consume a whole beam if it fits. Maximizes priority-weighted throughput.
* :class:`PriorityFairScheduler` — priority-aware *constrained* fairness: a per-request airtime
  cap gives bounded opportunity, so one poor-link / oversized request can't monopolize a beam;
  requests that don't fit within the cap are deferred (life-safety poor-link users are instead
  handled by the Tier-2 emergency lane, which has its own reservation + cap).

Disposition split (keeps the scheduler config-free): the scheduler decides *served* vs
*deferred* (capacity/geography) and drops only on a missed deadline. Retry-budget drops are the
control loop's job, since the budget is per-class config the scheduler doesn't carry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from satsim.domain.enums import RejectReason
from satsim.domain.models import (
    Allocation,
    Beam,
    ConstellationSnapshot,
    RejectedRequest,
    ScheduleCandidate,
    SchedulingResult,
    ServiceRequest,
)

_INF = float("inf")

# Default: a degradable request is served degraded only if at least this fraction of its (capped)
# cost can be allocated — below that the partial delivery isn't worth the airtime. Configurable.
_DEFAULT_DEGRADE_MIN_FRACTION = 0.25
_DEFAULT_PRIORITY_FAIR_CAP = 6.0


@dataclass(slots=True)
class _BeamState:
    """A beam plus the capacity still free this step."""

    beam: Beam
    remaining: float


def _jain_index(values: Sequence[float]) -> float:
    """Jain's fairness index over ``values`` including zero-service candidates."""
    if not values:
        return 1.0
    total = sum(values)
    if total <= 0.0:
        return 0.0
    sum_sq = sum(v * v for v in values)
    return (total * total) / (len(values) * sum_sq)


class _GreedyScheduler:
    """Highest-score-first greedy allocator with an optional per-request airtime cap."""

    def __init__(
        self,
        per_request_cap: float | None = None,
        degrade_min_fraction: float = _DEFAULT_DEGRADE_MIN_FRACTION,
    ) -> None:
        self._cap = per_request_cap if per_request_cap is not None else _INF
        self._degrade_min_fraction = degrade_min_fraction

    def schedule(
        self,
        candidates: Sequence[ScheduleCandidate],
        snapshot: ConstellationSnapshot,
        step: int,
    ) -> SchedulingResult:
        beams = [_BeamState(beam=b, remaining=b.capacity_units) for b in snapshot.beams]
        total_capacity = snapshot.total_capacity()

        allocations: list[Allocation] = []
        served: list[ServiceRequest] = []
        deferred: list[ServiceRequest] = []
        dropped: list[RejectedRequest] = []

        # Highest score first; request_id breaks ties for deterministic ordering.
        ordered = sorted(
            candidates, key=lambda c: (-c.scoring.score, c.request.request_id)
        )

        for candidate in ordered:
            request = candidate.request
            if request.is_expired(step):
                dropped.append(RejectedRequest(request, RejectReason.DEADLINE_MISSED))
                continue

            allocation = self._allocate(candidate, beams)
            if allocation is None:
                deferred.append(request)
            else:
                allocations.append(allocation)
                served.append(request)

        allocated_total = sum(a.allocated_units for a in allocations)
        utilization = allocated_total / total_capacity if total_capacity > 0.0 else 0.0
        allocated_by_request = {a.request_id: a.allocated_units for a in allocations}
        fairness = _jain_index(
            [allocated_by_request.get(c.request.request_id, 0.0) for c in candidates]
        )

        return SchedulingResult(
            allocations=tuple(allocations),
            served=tuple(served),
            deferred=tuple(deferred),
            dropped=tuple(dropped),
            fairness_index=fairness,
            utilization=min(1.0, utilization),
        )

    def _allocate(
        self, candidate: ScheduleCandidate, beams: list[_BeamState]
    ) -> Allocation | None:
        """Place a candidate, preferring a full-cost fit; degrade degradable ones if needed."""
        request = candidate.request
        need = candidate.scoring.estimated_cost_units
        # Regulatory airtime cap per legal (fleet, band) option.
        caps = {
            (opt.fleet, opt.band): _option_cap(opt.constraints.max_airtime_units)
            for opt in candidate.options
        }

        full_best: _BeamState | None = None
        full_headroom = -1.0
        degrade_best: _BeamState | None = None
        degrade_allocatable = 0.0
        for state in beams:
            beam = state.beam
            reg_cap = caps.get((beam.fleet, beam.band))
            if reg_cap is None or beam.region != request.region:
                continue  # not a legal option, or wrong geography
            allocatable = min(state.remaining, reg_cap, self._cap)
            if allocatable + 1e-9 >= need:
                if state.remaining > full_headroom:  # full fit: pick most headroom
                    full_best = state
                    full_headroom = state.remaining
            elif allocatable > degrade_allocatable:  # partial fit, for possible degradation
                degrade_best = state
                degrade_allocatable = allocatable

        if full_best is not None:
            full_best.remaining -= need
            return self._grant(request, candidate, full_best, need, degraded=False)

        if candidate.degradable and degrade_best is not None:
            floor = self._degrade_min_fraction * min(need, self._cap)
            if degrade_allocatable + 1e-9 >= floor:
                amount = min(degrade_allocatable, need)
                degrade_best.remaining -= amount
                return self._grant(request, candidate, degrade_best, amount, degraded=True)

        return None

    @staticmethod
    def _grant(
        request: ServiceRequest,
        candidate: ScheduleCandidate,
        state: _BeamState,
        units: float,
        *,
        degraded: bool,
    ) -> Allocation:
        return Allocation(
            request_id=request.request_id,
            fleet=state.beam.fleet,
            beam_id=state.beam.beam_id,
            band=state.beam.band,
            allocated_units=units,
            delivery_probability=candidate.scoring.delivery_probability,
            degraded=degraded,
        )


def _option_cap(max_airtime_units: float | None) -> float:
    return max_airtime_units if max_airtime_units is not None else _INF


class HeuristicScheduler(_GreedyScheduler):
    """Priority + weighted-fair: greedy by score, no per-request airtime cap."""

    def __init__(
        self, degrade_min_fraction: float = _DEFAULT_DEGRADE_MIN_FRACTION
    ) -> None:
        super().__init__(per_request_cap=None, degrade_min_fraction=degrade_min_fraction)


class PriorityFairScheduler(_GreedyScheduler):
    """Priority-aware constrained fairness: greedy by score under a per-request airtime cap."""

    def __init__(
        self,
        max_units_per_request: float = _DEFAULT_PRIORITY_FAIR_CAP,
        degrade_min_fraction: float = _DEFAULT_DEGRADE_MIN_FRACTION,
    ) -> None:
        super().__init__(
            per_request_cap=max_units_per_request, degrade_min_fraction=degrade_min_fraction
        )
