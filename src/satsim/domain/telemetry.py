"""Telemetry value types: structured per-decision events and per-step aggregates.

Emitting these (via :class:`satsim.ports.telemetry.TelemetrySink`) is what makes the
behavior of a heterogeneous, changing backend legible: who got served, who got
dropped, why, on which fleet, and how close we are to congestion collapse.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from satsim.domain.enums import (
    DegradeMode,
    FleetId,
    Region,
    RejectReason,
    TrafficClass,
)

# Permitted value types for free-form event detail (keeps events JSON-serializable).
DetailValue = str | int | float | bool


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """A single structured decision event (admit/reject/serve/fallback/...).

    ``kind`` is a short stable string (e.g. ``"admit"``, ``"reject"``, ``"served"``,
    ``"deferred"``, ``"dropped"``, ``"fallback"``, ``"circuit_breaker"``,
    ``"optimizer_run"``). Optional fields are populated when relevant.
    """

    step: int
    kind: str
    request_id: str | None = None
    traffic_class: TrafficClass | None = None
    region: Region | None = None
    fleet: FleetId | None = None
    reason: RejectReason | None = None
    detail: Mapping[str, DetailValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StepCounters:
    """Aggregate counters for one step, plus fairness/congestion/resilience indicators."""

    step: int
    admitted: int = 0
    rejected: int = 0
    served: int = 0
    deferred: int = 0
    dropped: int = 0
    degraded: int = 0  # served, but at a reduced allocation (degradable classes)

    served_by_class: Mapping[TrafficClass, int] = field(default_factory=dict)
    served_by_region: Mapping[Region, int] = field(default_factory=dict)
    served_by_fleet: Mapping[FleetId, int] = field(default_factory=dict)
    rejected_by_reason: Mapping[RejectReason, int] = field(default_factory=dict)
    # Wait (in steps, = served_step - arrival_step) of every request served this step. Per-request
    # so a run can compute latency percentiles (p95/p99), not just an average.
    served_wait_steps: tuple[int, ...] = ()

    fairness_index: float = 1.0  # Jain's index across candidate opportunity [0,1]
    utilization: float = 0.0  # fraction of capacity allocated [0,1]
    collapse_risk: float = 0.0  # congestion-collapse indicator [0,1]
    queue_depth: int = 0  # retry/source backlog visible to control and telemetry
    # Offered best-effort load vs schedulable capacity this step (airtime units): lets a run tell
    # demand-limited idle ("nobody asked") from wasted idle ("turned demand away with room").
    offered_load_units: float = 0.0
    available_capacity_units: float = 0.0

    degrade_mode: DegradeMode = DegradeMode.NORMAL
    fallback_activations: int = 0
    circuit_breaker_trips: int = 0
