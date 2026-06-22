"""MAC-layer scheduler: a real slot x subchannel resource-block grid with link adaptation.

Where the other schedulers treat a beam as a single fluid ``capacity_units`` number, the
``SlotMacScheduler`` expands each beam into a grid of **resource blocks (RBs)** — one cell per
(time slot, frequency subchannel) — and assigns whole RBs to requests. How many RBs a request
needs is set by **link adaptation**: its payload size in bits divided by the bits an RB carries at
the MCS its link quality supports (better link → higher MCS → more bits/RB → fewer RBs).

It satisfies the same :class:`~satsim.ports.scheduler.ResourceScheduler` port and returns the same
:class:`SchedulingResult`, so the control loop is unchanged — capacity is just accounted at RB
granularity instead of as a scalar. Allocations are mapped back to ``capacity_units`` (a beam's
capacity split evenly across its RBs) so utilization/fairness stay comparable across schedulers.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from satsim.config import MacConfig
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
        needed = self._rbs_needed(request)
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
            reg_cap_rbs = self._regulatory_cap_rbs(option_cap, grid.rb_units, grid.free_rbs)
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

    def _rbs_needed(self, request: ServiceRequest) -> int:
        bits = request.size_bytes * 8
        bits_per_rb = self._config.symbols_per_rb * self._spectral_efficiency(request.link_quality)
        if bits_per_rb <= 0.0:
            return 1
        return max(1, math.ceil(bits / bits_per_rb))

    def _spectral_efficiency(self, link_quality: float) -> float:
        """Bits/symbol of the highest MCS tier the link supports (falls back to the lowest)."""
        efficiency = self._mcs[0].bits_per_symbol
        for level in self._mcs:
            if link_quality >= level.min_link_quality:
                efficiency = level.bits_per_symbol
        return efficiency

    @staticmethod
    def _regulatory_cap_rbs(
        max_airtime_units: float | None, rb_units: float, free_rbs: int
    ) -> int:
        if rb_units <= 0.0:
            return 0
        if max_airtime_units is None:
            return free_rbs
        return max(0, math.floor(max_airtime_units / rb_units))
