"""Tier-2 / Tier-3 planning types.

These describe the *plan* the periodic global optimizer (Tier 3) broadcasts to the
per-step loop, and the decision the reactive emergency lane (Tier 2) returns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from satsim.domain.enums import FleetId, Region, TrafficClass
from satsim.domain.models import (
    Allocation,
    CongestionState,
    ConstellationSnapshot,
    RejectedRequest,
    ServiceRequest,
)
from satsim.domain.telemetry import StepCounters


@dataclass(frozen=True, slots=True)
class AdmissionCurve:
    """A monotone score -> admit-probability mapping, broadcast by Tier 3.

    Represented as sorted ``(score, probability)`` breakpoints with linear
    interpolation between them; both axes are in ``[0, 1]``. As congestion rises the
    optimizer shifts the curve down/right to shed more load.
    """

    breakpoints: tuple[tuple[float, float], ...]

    @classmethod
    def constant(cls, probability: float = 1.0) -> AdmissionCurve:
        """A flat curve that admits everything with the given probability."""
        return cls(breakpoints=((0.0, probability), (1.0, probability)))

    def admit_probability(self, score: float) -> float:
        """Interpolated admit probability for a request ``score`` in ``[0, 1]``."""
        pts = self.breakpoints
        if not pts:
            return 1.0
        if score <= pts[0][0]:
            return pts[0][1]
        if score >= pts[-1][0]:
            return pts[-1][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
            if x0 <= score <= x1:
                if x1 == x0:
                    return y1
                t = (score - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)
        return pts[-1][1]


@dataclass(frozen=True, slots=True)
class PlanningWindow:
    """Input to :meth:`satsim.ports.optimizer.Optimizer.plan` — the global view.

    Aggregates the recent past (congestion history, demand mix) with the current
    capacity so the optimizer can plan for the next cadence window.
    """

    step: int
    snapshot: ConstellationSnapshot  # merged current capacity across fleets
    recent_congestion: tuple[CongestionState, ...] = ()
    recent_demand_units: Mapping[TrafficClass, float] = field(default_factory=dict)
    # Per-(region, class) recent demand in airtime units — lets the solver allocate budgets
    # per region and weight fleet assignment by where demand actually is.
    recent_demand_by_region: Mapping[tuple[Region, TrafficClass], float] = field(
        default_factory=dict
    )
    # Recent per-step aggregates (served/dropped by class/region/fleet) — the signal the
    # optimizer needs to recompute fairness weights, not just the congestion scalar.
    recent_counters: tuple[StepCounters, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourcePlan:
    """The plan the per-step control loop consults until the next Tier-3 run.

    All fields are advisory inputs the loop *consults*; Tier 1 remains authoritative.
    """

    generated_at_step: int
    valid_for_steps: int  # cadence: how long this plan is intended to apply
    admission_curve: AdmissionCurve
    fairness_weights: Mapping[TrafficClass, float] = field(default_factory=dict)
    airtime_budgets: Mapping[TrafficClass, float] = field(default_factory=dict)
    # Per-(region, class) budgets from the coupled solver (empty for the heuristic planner).
    region_class_budgets: Mapping[tuple[Region, TrafficClass], float] = field(default_factory=dict)
    fleet_hints: Mapping[Region, FleetId] = field(default_factory=dict)

    @classmethod
    def passthrough(cls, step: int = 0, valid_for_steps: int = 1) -> ResourcePlan:
        """A neutral plan (admit everything, no per-class caps) for bootstrapping."""
        return cls(
            generated_at_step=step,
            valid_for_steps=valid_for_steps,
            admission_curve=AdmissionCurve.constant(1.0),
        )


@dataclass(frozen=True, slots=True)
class EmergencyDecision:
    """Output of the Tier-2 reactive emergency lane for a single step.

    The lane runs emergency-class admission control within bounded reserved per-beam
    capacity: ``admitted`` urgent requests claim ``reservations``; ``shed`` urgent
    requests fall through to the normal best-effort path or are queued/dropped.
    """

    admitted: tuple[ServiceRequest, ...] = ()
    reservations: tuple[Allocation, ...] = ()
    shed: tuple[RejectedRequest, ...] = ()
    reserved_units_used: float = 0.0
