"""MAC-layer schedulers: a real slot x subchannel resource-block grid with link adaptation.

Where the fluid schedulers treat a beam as a single ``capacity_units`` number, both schedulers
here expand each beam into a grid of **resource blocks (RBs)** — one cell per (time slot, frequency
subchannel). How many RBs a request needs is set by **link adaptation**: its payload size in bits
divided by the bits an RB carries at the MCS its link quality supports (better link → higher MCS →
more bits/RB → fewer RBs). Two contrasting access models share that grid:

* :class:`SlotMacScheduler` — **granted** access: a central allocator assigns whole RBs greedily,
  highest-score first, collision-free by construction.
* :class:`ContentionMacScheduler` — **random** access (slotted ALOHA): every UE independently picks
  RBs at random, so collisions happen and defer the colliders. ``capture_capacity`` models the
  spreading-code recovery of US 11,848,747 — up to *C* UEs per RB are separable before it overloads.

Both satisfy the :class:`~satsim.ports.scheduler.ResourceScheduler` port and return the same
:class:`SchedulingResult`, so the control loop is unchanged — capacity is just accounted at RB
granularity instead of as a scalar. Allocations are mapped back to ``capacity_units`` (a beam's
capacity split evenly across its RBs) so utilization/fairness stay comparable across schedulers.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from satsim.config import MacConfig, McsLevel
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
from satsim.rng import Rng

_INF = float("inf")


def _spectral_efficiency(mcs: Sequence[McsLevel], link_quality: float) -> float:
    """Bits/symbol of the highest MCS tier the link supports (falls back to the lowest)."""
    efficiency = mcs[0].bits_per_symbol
    for level in mcs:
        if link_quality >= level.min_link_quality:
            efficiency = level.bits_per_symbol
    return efficiency


def _rbs_needed(symbols_per_rb: int, mcs: Sequence[McsLevel], request: ServiceRequest) -> int:
    """Resource blocks to carry ``request``'s payload at the MCS its link quality supports."""
    bits = request.size_bytes * 8
    bits_per_rb = symbols_per_rb * _spectral_efficiency(mcs, request.link_quality)
    if bits_per_rb <= 0.0:
        return 1
    return max(1, math.ceil(bits / bits_per_rb))


def _regulatory_cap_rbs(max_airtime_units: float | None, rb_units: float, unlimited: int) -> int:
    """Max RBs a regulatory airtime cap permits (``unlimited`` when the option has no cap)."""
    if rb_units <= 0.0:
        return 0
    if max_airtime_units is None:
        return unlimited
    return max(0, math.floor(max_airtime_units / rb_units))


@dataclass(slots=True)
class _BeamGrid:
    """A beam plus how many of its resource blocks are still free this step."""

    beam: Beam
    free_rbs: int
    rb_units: float  # capacity_units carried by one RB (for mapping back to airtime)


def _jain_index(values: Sequence[float]) -> float:
    if not values:
        return 1.0
    total = sum(values)
    if total <= 0.0:
        return 0.0
    return (total * total) / (len(values) * sum(v * v for v in values))


class SlotMacScheduler:
    """Assigns slot x subchannel resource blocks to requests with MCS-based link adaptation."""

    def __init__(self, config: MacConfig) -> None:
        self._config = config
        self._rbs_per_beam = config.resource_blocks_per_beam
        # MCS ladder sorted by threshold ascending, for a simple highest-supported lookup.
        self._mcs = sorted(config.mcs_table, key=lambda m: m.min_link_quality)

    def schedule(
        self,
        candidates: Sequence[ScheduleCandidate],
        snapshot: ConstellationSnapshot,
        step: int,
    ) -> SchedulingResult:
        grids: list[_BeamGrid] = [
            _BeamGrid(
                beam=b,
                free_rbs=self._rbs_per_beam,
                rb_units=b.capacity_units / self._rbs_per_beam if self._rbs_per_beam else 0.0,
            )
            for b in snapshot.beams
        ]
        total_capacity = snapshot.total_capacity()

        allocations: list[Allocation] = []
        served: list[ServiceRequest] = []
        deferred: list[ServiceRequest] = []
        dropped: list[RejectedRequest] = []

        ordered = sorted(candidates, key=lambda c: (-c.scoring.score, c.request.request_id))
        for candidate in ordered:
            request = candidate.request
            if request.is_expired(step):
                dropped.append(RejectedRequest(request, RejectReason.DEADLINE_MISSED))
                continue
            allocation = self._assign(candidate, grids)
            if allocation is None:
                deferred.append(request)
            else:
                allocations.append(allocation)
                served.append(request)

        allocated_units = sum(a.allocated_units for a in allocations)
        utilization = allocated_units / total_capacity if total_capacity > 0.0 else 0.0
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

    def _assign(self, candidate: ScheduleCandidate, grids: list[_BeamGrid]) -> Allocation | None:
        request = candidate.request
        needed = _rbs_needed(self._config.symbols_per_rb, self._mcs, request)
        cap = self._config.max_rbs_per_request or needed  # per-request RB cap (MAC fairness)
        option_caps = {
            (opt.fleet, opt.band): opt.constraints.max_airtime_units for opt in candidate.options
        }

        # Pick the eligible beam offering the most assignable RBs.
        best: _BeamGrid | None = None
        best_assignable = 0
        for grid in grids:
            beam = grid.beam
            option_cap = option_caps.get((beam.fleet, beam.band))
            if beam.region != request.region or (beam.fleet, beam.band) not in option_caps:
                continue
            reg_cap_rbs = _regulatory_cap_rbs(option_cap, grid.rb_units, grid.free_rbs)
            assignable = min(grid.free_rbs, cap, reg_cap_rbs)
            if assignable > best_assignable:
                best = grid
                best_assignable = assignable

        if best is None or best_assignable <= 0:
            return None

        # Full fit: the request needs no more than its cap and the beam has room for all of it.
        if needed <= cap and best_assignable >= needed:
            return self._grant(request, candidate, best, needed, degraded=False)

        # Otherwise only degradable classes accept fewer RBs than they need (cap or grid limited).
        floor = math.ceil(self._config.degrade_min_fraction * min(needed, cap))
        if candidate.degradable and best_assignable >= floor:
            return self._grant(request, candidate, best, best_assignable, degraded=True)
        return None

    def _grant(
        self,
        request: ServiceRequest,
        candidate: ScheduleCandidate,
        grid: _BeamGrid,
        rbs: int,
        *,
        degraded: bool,
    ) -> Allocation:
        grid.free_rbs -= rbs
        return Allocation(
            request_id=request.request_id,
            fleet=grid.beam.fleet,
            beam_id=grid.beam.beam_id,
            band=grid.beam.band,
            allocated_units=rbs * grid.rb_units,
            delivery_probability=min(1.0, max(0.0, request.link_quality)),
            degraded=degraded,
        )


@dataclass(slots=True)
class _Attempt:
    """One UE's random-access transmission this step: the RBs it grabbed in a beam."""

    candidate: ScheduleCandidate
    beam: Beam
    rbs: frozenset[int]
    degraded: bool


class ContentionMacScheduler:
    """Slotted random-access uplink: UEs contend for RBs and collisions defer (slotted ALOHA).

    Where :class:`SlotMacScheduler` *grants* collision-free RBs from a central scheduler, this
    models the opposite, uncoordinated regime of Talakoub et al.'s "multiple user access channel"
    (US 11,848,747): every admitted UE independently picks one eligible beam and a random set of
    resource blocks within the step's window, then transmits. The gateway recovers only RBs
    occupied by exactly one UE; any RB chosen by two or more **collides**, and every packet
    touching a collided RB fails and is deferred to retry (the control loop applies the backoff).

    Throughput therefore traces the classic ALOHA curve — it rises with offered load, peaks, then
    *collapses* as collisions dominate — which a granted scheduler hides by construction. Random
    selection is drawn from an injected seeded :class:`~satsim.rng.Rng`, so runs stay reproducible;
    link adaptation and regulatory/RB caps are shared with :class:`SlotMacScheduler`.

    ``config.capture_capacity`` models the patent's collision *tolerance*: with spreading codes the
    gateway can separate up to ``C`` UEs sharing one RB, so an RB only overloads (and defers all
    its packets) beyond ``C`` simultaneous transmissions. ``C = 1`` is pure slotted ALOHA; raising
    it recovers colliders and cuts deferrals, at the cost of splitting the shared RB's airtime
    among its occupants (the spreading-gain rate tradeoff). Capacity accounting stays exact.
    """

    def __init__(self, config: MacConfig, rng: Rng) -> None:
        self._config = config
        self._rng = rng
        self._rbs_per_beam = config.resource_blocks_per_beam
        self._capture = config.capture_capacity
        # MCS ladder sorted ascending for a simple highest-supported lookup.
        self._mcs = sorted(config.mcs_table, key=lambda m: m.min_link_quality)

    def schedule(
        self,
        candidates: Sequence[ScheduleCandidate],
        snapshot: ConstellationSnapshot,
        step: int,
    ) -> SchedulingResult:
        beams = snapshot.beams
        rb_units = {
            b.beam_id: (b.capacity_units / self._rbs_per_beam if self._rbs_per_beam else 0.0)
            for b in beams
        }
        total_capacity = snapshot.total_capacity()

        dropped: list[RejectedRequest] = []
        deferred: list[ServiceRequest] = []

        # Pass 1 — every viable UE picks a beam and a random RB set and "transmits". Draw order is
        # request_id (deterministic); the rng, not arrival order, supplies the randomness.
        attempts: list[_Attempt] = []
        occupancy: dict[tuple[str, int], int] = {}
        for candidate in sorted(candidates, key=lambda c: c.request.request_id):
            request = candidate.request
            if request.is_expired(step):
                dropped.append(RejectedRequest(request, RejectReason.DEADLINE_MISSED))
                continue
            attempt = self._attempt(candidate, beams, rb_units)
            if attempt is None:
                deferred.append(request)
                continue
            attempts.append(attempt)
            for rb in attempt.rbs:
                key = (attempt.beam.beam_id, rb)
                occupancy[key] = occupancy.get(key, 0) + 1

        # Pass 2 — a UE succeeds iff none of its chosen RBs was over-subscribed beyond the capture
        # capacity; a recovered RB's airtime is split among the UEs sharing it (spreading tradeoff).
        allocations: list[Allocation] = []
        served: list[ServiceRequest] = []
        for attempt in attempts:
            beam_id = attempt.beam.beam_id
            if any(occupancy[(beam_id, rb)] > self._capture for rb in attempt.rbs):
                deferred.append(attempt.candidate.request)
            else:
                units = sum(rb_units[beam_id] / occupancy[(beam_id, rb)] for rb in attempt.rbs)
                allocations.append(self._grant(attempt, units))
                served.append(attempt.candidate.request)

        allocated_units = sum(a.allocated_units for a in allocations)
        utilization = allocated_units / total_capacity if total_capacity > 0.0 else 0.0
        by_request = {a.request_id: a.allocated_units for a in allocations}
        fairness = _jain_index([by_request.get(c.request.request_id, 0.0) for c in candidates])

        return SchedulingResult(
            allocations=tuple(allocations),
            served=tuple(served),
            deferred=tuple(deferred),
            dropped=tuple(dropped),
            fairness_index=fairness,
            utilization=min(1.0, utilization),
        )

    def _attempt(
        self, candidate: ScheduleCandidate, beams: Sequence[Beam], rb_units: dict[str, float]
    ) -> _Attempt | None:
        """Pick one eligible beam at random and the RB set this UE will transmit on."""
        eligible = [b for b in beams if self._eligible(b, candidate)]
        if not eligible:
            return None
        beam = eligible[self._rng.randint(0, len(eligible) - 1)]
        k, degraded = self._rbs_for(candidate, beam, rb_units[beam.beam_id])
        if k <= 0:
            return None
        return _Attempt(candidate=candidate, beam=beam, rbs=self._sample_rbs(k), degraded=degraded)

    def _rbs_for(
        self, candidate: ScheduleCandidate, beam: Beam, rb_units: float
    ) -> tuple[int, bool]:
        """RBs this UE attempts (and whether that is a degraded, below-need amount)."""
        needed = _rbs_needed(self._config.symbols_per_rb, self._mcs, candidate.request)
        cap = self._config.max_rbs_per_request or needed
        option_cap = next(
            (
                opt.constraints.max_airtime_units
                for opt in candidate.options
                if opt.fleet == beam.fleet and opt.band == beam.band
            ),
            None,
        )
        reg = _regulatory_cap_rbs(option_cap, rb_units, self._rbs_per_beam)
        k = min(needed, cap, reg, self._rbs_per_beam)
        if k >= needed:
            return needed, False
        # Capped below need: only degradable classes still attempt (with fewer RBs).
        if candidate.degradable:
            floor = math.ceil(self._config.degrade_min_fraction * min(needed, cap))
            if k >= floor:
                return k, True
        return 0, False

    def _sample_rbs(self, k: int) -> frozenset[int]:
        """Draw ``k`` distinct RB indices uniformly from the beam's grid."""
        n = self._rbs_per_beam
        k = min(k, n)
        chosen: set[int] = set()
        while len(chosen) < k:
            chosen.add(self._rng.randint(0, n - 1))
        return frozenset(chosen)

    @staticmethod
    def _eligible(beam: Beam, candidate: ScheduleCandidate) -> bool:
        if beam.region != candidate.request.region:
            return False
        return any(
            opt.fleet == beam.fleet and opt.band == beam.band for opt in candidate.options
        )

    @staticmethod
    def _grant(attempt: _Attempt, allocated_units: float) -> Allocation:
        request = attempt.candidate.request
        return Allocation(
            request_id=request.request_id,
            fleet=attempt.beam.fleet,
            beam_id=attempt.beam.beam_id,
            band=attempt.beam.band,
            allocated_units=allocated_units,
            delivery_probability=min(1.0, max(0.0, request.link_quality)),
            degraded=attempt.degraded,
        )
