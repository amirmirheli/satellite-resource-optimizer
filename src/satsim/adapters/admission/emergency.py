"""Default :class:`~satsim.ports.admission.EmergencyAdmission`: the Tier-2 reactive lane.

Runs before best-effort admission each step. It reserves a bounded fraction of every beam's
capacity for urgent traffic and runs *emergency-class admission control* over that reserved
pool: requests are ranked by severity, waiting time, and retry count, priced with the shared
airtime cost model, capped per request (bounded opportunity so a poor-link user can't drain a
beam), and bounded per region (geographic fairness so one region's surge can't take more than
its fair share of the reserved pool).

Requests that don't fit are *shed* — they fall through to Tier-1 admission as ordinary
high-scored load (Tier-2-first, Tier-1-spillover). Because the lane only ever touches the
reserved fraction, a mass-casualty surge can saturate the reserved pool without starving the
best-effort capacity the scheduler still allocates from.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from satsim.config import EmergencyConfig, ScoringConfig
from satsim.domain.enums import Region, RejectReason
from satsim.domain.models import (
    Allocation,
    Beam,
    ConstellationSnapshot,
    RegulatoryDecision,
    RejectedRequest,
    ServiceRequest,
)
from satsim.domain.planning import EmergencyDecision, ResourcePlan
from satsim.ports.regulatory import RegulatoryPolicy
from satsim.scoring import estimate_cost_units


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass(slots=True)
class _Reserved:
    """A beam plus the reserved capacity still free for urgent traffic this step."""

    beam: Beam
    remaining: float


class EmergencyLane:
    """Tier-2 reactive emergency lane over a reserved slice of beam capacity."""

    def __init__(
        self,
        config: EmergencyConfig,
        regulatory: RegulatoryPolicy,
        scoring: ScoringConfig | None = None,
    ) -> None:
        self._config = config
        self._regulatory = regulatory
        self._scoring = scoring if scoring is not None else ScoringConfig()

    def reserve(
        self,
        urgent: Sequence[ServiceRequest],
        snapshot: ConstellationSnapshot,
        plan: ResourcePlan,
        step: int,
    ) -> EmergencyDecision:
        pool = [
            _Reserved(beam=b, remaining=b.capacity_units * self._config.reserved_fraction)
            for b in snapshot.beams
            if b.capacity_units * self._config.reserved_fraction > 0.0
        ]

        admitted: list[ServiceRequest] = []
        reservations: list[Allocation] = []
        shed: list[RejectedRequest] = []

        ranked = self._rank(urgent, shed, step)

        total_reserved = sum(r.remaining for r in pool)
        regions = {req.region for _, req in ranked}
        per_region_cap = self._per_region_cap(total_reserved, len(regions))
        region_used: dict[Region, float] = {}
        used = 0.0

        for _score, request in ranked:
            cost = estimate_cost_units(
                request.size_bytes, request.link_quality,
                self._scoring.bytes_per_unit, self._scoring.min_link_quality,
            )
            if cost > self._config.max_units_per_request + 1e-9:
                shed.append(RejectedRequest(request, RejectReason.EMERGENCY_LANE_FULL))
                continue
            need = cost
            if region_used.get(request.region, 0.0) + need > per_region_cap + 1e-9:
                shed.append(RejectedRequest(request, RejectReason.EMERGENCY_LANE_FULL))
                continue

            allocation = self._place(request, need, pool)
            if allocation is None:
                shed.append(RejectedRequest(request, RejectReason.EMERGENCY_LANE_FULL))
                continue

            admitted.append(request)
            reservations.append(allocation)
            region_used[request.region] = region_used.get(request.region, 0.0) + need
            used += need

        return EmergencyDecision(
            admitted=tuple(admitted),
            reservations=tuple(reservations),
            shed=tuple(shed),
            reserved_units_used=used,
        )

    def _rank(
        self,
        urgent: Sequence[ServiceRequest],
        shed: list[RejectedRequest],
        step: int,
    ) -> list[tuple[float, ServiceRequest]]:
        """Drop expired / over-budget requests; score the rest, highest urgency first."""
        ranked: list[tuple[float, ServiceRequest]] = []
        for request in urgent:
            if request.is_expired(step):
                shed.append(RejectedRequest(request, RejectReason.DEADLINE_MISSED))
                continue
            if request.retry_count > self._config.max_retries:
                shed.append(RejectedRequest(request, RejectReason.RETRY_BUDGET_EXHAUSTED))
                continue
            ranked.append((self._emergency_score(request, step), request))
        ranked.sort(key=lambda sr: (-sr[0], sr[1].request_id))
        return ranked

    def _emergency_score(self, request: ServiceRequest, step: int) -> float:
        cfg = self._config
        severity = _clamp(request.urgency)
        span = max(1, request.deadline_step - request.arrival_step)
        waiting = _clamp(request.waiting_steps(step) / span)
        retry = _clamp(request.retry_count / max(1, cfg.max_retries))
        return (
            cfg.severity_weight * severity
            + cfg.waiting_weight * waiting
            + cfg.retry_weight * retry
        )

    def _per_region_cap(self, total_reserved: float, region_count: int) -> float:
        if region_count <= 0:
            return 0.0
        equal_share = total_reserved / region_count
        return min(total_reserved, equal_share * self._config.geo_fairness_weight)

    def _place(
        self, request: ServiceRequest, need: float, pool: list[_Reserved]
    ) -> Allocation | None:
        """Reserve ``need`` units on the eligible beam with the most reserved headroom."""
        best: _Reserved | None = None
        for state in pool:
            beam = state.beam
            if beam.region != request.region:
                continue
            decision = self._regulatory.evaluate(request.region, beam.fleet, beam.band)
            if not decision.allowed:
                continue
            if min(state.remaining, _reg_cap(decision)) + 1e-9 < need:
                continue
            if best is None or state.remaining > best.remaining:
                best = state

        if best is None:
            return None
        best.remaining -= need
        return Allocation(
            request_id=request.request_id,
            fleet=best.beam.fleet,
            beam_id=best.beam.beam_id,
            band=best.beam.band,
            allocated_units=need,
            delivery_probability=_clamp(request.link_quality),
        )


def _reg_cap(decision: RegulatoryDecision) -> float:
    if decision.constraints is None or decision.constraints.max_airtime_units is None:
        return float("inf")
    return decision.constraints.max_airtime_units
