"""Core domain models exchanged across port boundaries.

All models are frozen, slotted dataclasses: immutable value objects with no behavior
beyond a few derived read-only properties. Anything that *acts* lives behind a port.

Conventions:
    * Capacity / size are expressed in abstract **airtime units** (and ``size_bytes``
      for payloads); the simulation is unit-agnostic, only ratios matter.
    * Time is measured in integer **steps**; ``deadline_step`` is absolute.
    * Probabilities and quality scores are floats in ``[0.0, 1.0]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from satsim.domain.enums import Band, FleetId, Outcome, Region, RejectReason, TrafficClass

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceRequest:
    """A single unit of demand entering the system.

    A request is not a long-lived connection: it is decided each step and, if not
    served, may be deferred and carried to a later step (subject to ``retry_count``
    against a configured retry budget) until served, dropped, or its deadline passes.
    """

    request_id: str
    traffic_class: TrafficClass
    region: Region
    size_bytes: int
    link_quality: float  # [0,1] proxy for probability of a clean link / few retransmits
    urgency: float  # [0,1] within-class severity (e.g. emergency triage signal)
    arrival_step: int
    deadline_step: int  # absolute step by which the request must be served
    retry_count: int = 0  # number of prior failed/deferred attempts
    preferred_band: Band | None = None

    def waiting_steps(self, current_step: int) -> int:
        """Steps elapsed since arrival (>= 0)."""
        return max(0, current_step - self.arrival_step)

    def is_expired(self, current_step: int) -> bool:
        """True once the deadline has passed."""
        return current_step > self.deadline_step


# ---------------------------------------------------------------------------
# Constellation resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Beam:
    """A single steerable beam on a satellite, with capacity available this step."""

    beam_id: str
    fleet: FleetId
    satellite_id: str
    region: Region  # the region this beam currently illuminates
    band: Band
    capacity_units: float  # allocatable airtime units remaining this step


@dataclass(frozen=True, slots=True)
class Satellite:
    """A satellite currently in view, exposing zero or more beams."""

    satellite_id: str
    fleet: FleetId
    beams: tuple[Beam, ...] = ()


@dataclass(frozen=True, slots=True)
class ConstellationSnapshot:
    """Point-in-time view of one or more fleets at a given step.

    Returned by :class:`satsim.ports.constellation.ConstellationClient` per fleet;
    the control loop merges per-fleet snapshots before scheduling.
    """

    step: int
    satellites: tuple[Satellite, ...] = ()

    @property
    def beams(self) -> tuple[Beam, ...]:
        """All beams across all visible satellites in this snapshot."""
        return tuple(beam for sat in self.satellites for beam in sat.beams)

    def total_capacity(self) -> float:
        """Aggregate allocatable capacity across every beam this step."""
        return sum(beam.capacity_units for beam in self.beams)


# ---------------------------------------------------------------------------
# Regulatory
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegulatoryConstraints:
    """Per-region limits attached to an allowed (fleet, band) option.

    ``None`` means "no explicit limit". Limits are expressed in the same abstract
    units as beam capacity / power budget.
    """

    max_airtime_units: float | None = None
    max_power: float | None = None


@dataclass(frozen=True, slots=True)
class RegulatoryDecision:
    """Result of evaluating a single (region, fleet, band) triple."""

    allowed: bool
    constraints: RegulatoryConstraints | None = None
    reason: str | None = None  # human-readable denial reason when ``allowed`` is False


@dataclass(frozen=True, slots=True)
class CandidateOption:
    """A legal (fleet, band) option for a request, with its regulatory constraints."""

    fleet: FleetId
    band: Band
    constraints: RegulatoryConstraints = field(default_factory=RegulatoryConstraints)


# ---------------------------------------------------------------------------
# Scheduling inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestScore:
    """The single, authoritative scoring of a request for one step.

    Produced once by ``satsim.scoring.RequestScorer`` and reused everywhere so the
    admission stage, the congestion estimate, and the scheduler never disagree about a
    request's score or cost. ``score`` is a composite urgency/priority/waiting value in
    ``[0, 1]`` (fed straight into the admission curve and used for scheduler ordering).
    """

    score: float  # [0,1] composite urgency/priority/waiting
    estimated_cost_units: float  # expected airtime to serve (drives efficiency tradeoffs)
    delivery_probability: float  # [0,1] expected probability of successful delivery


@dataclass(frozen=True, slots=True)
class ScoredRequest:
    """A request paired with its authoritative :class:`RequestScore`.

    The control loop scores each request once (via ``satsim.scoring.RequestScorer``) and
    passes ``ScoredRequest``\\ s through admission, so the admission stage never recomputes
    a score and can't disagree with the scheduler about one.
    """

    request: ServiceRequest
    scoring: RequestScore


@dataclass(frozen=True, slots=True)
class ScheduleCandidate:
    """An admitted, region-filtered request handed to the scheduler.

    Carries the precomputed :class:`RequestScore` and the set of legal ``options`` so
    the scheduler re-runs neither admission, regulatory, nor scoring logic.
    """

    request: ServiceRequest
    options: tuple[CandidateOption, ...]
    scoring: RequestScore
    degradable: bool = False  # may be served at a reduced allocation when full cost won't fit


@dataclass(frozen=True, slots=True)
class Allocation:
    """A granted assignment of beam resources to a request."""

    request_id: str
    fleet: FleetId
    beam_id: str
    band: Band
    allocated_units: float
    delivery_probability: float  # [0,1] expected probability of successful delivery
    degraded: bool = False  # served below full cost (degradable class under scarcity)


@dataclass(frozen=True, slots=True)
class RejectedRequest:
    """A request paired with the reason it was rejected/shed/dropped."""

    request: ServiceRequest
    reason: RejectReason


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """Output of the admission stage: what to schedule vs. what was shed.

    Admitted requests keep their :class:`RequestScore` (as :class:`ScoredRequest`) so the
    loop can build :class:`ScheduleCandidate`\\ s without rescoring.
    """

    admitted: tuple[ScoredRequest, ...] = ()
    rejected: tuple[RejectedRequest, ...] = ()


@dataclass(frozen=True, slots=True)
class SchedulingResult:
    """Output of the scheduler for a single step."""

    allocations: tuple[Allocation, ...] = ()
    served: tuple[ServiceRequest, ...] = ()
    deferred: tuple[ServiceRequest, ...] = ()
    dropped: tuple[RejectedRequest, ...] = ()
    fairness_index: float = 1.0  # e.g. Jain's index across served requests [0,1]
    utilization: float = 0.0  # fraction of available capacity allocated [0,1]


# ---------------------------------------------------------------------------
# Congestion / control signals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CongestionState:
    """Snapshot of congestion the admission controller and optimizer react to."""

    step: int
    queue_depth: int  # backlog carried into this step (consumer lag)
    offered_load_units: float  # estimated resource demand this step
    available_capacity_units: float  # aggregate beam capacity this step
    drop_rate: float = 0.0  # recent fraction of requests dropped [0,1]

    @property
    def utilization(self) -> float:
        """Offered load as a fraction of available capacity (can exceed 1.0)."""
        if self.available_capacity_units <= 0.0:
            return float("inf") if self.offered_load_units > 0.0 else 0.0
        return self.offered_load_units / self.available_capacity_units

    @property
    def collapse_risk(self) -> float:
        """Coarse [0,1] indicator combining over-subscription and drop rate."""
        over = max(0.0, self.utilization - 1.0)
        return min(1.0, 0.5 * min(1.0, over) + 0.5 * self.drop_rate)


@dataclass(frozen=True, slots=True)
class RequestDisposition:
    """Final per-request record for telemetry: what happened and (if rejected) why."""

    request_id: str
    traffic_class: TrafficClass
    region: Region
    outcome: Outcome
    fleet: FleetId | None = None
    reason: RejectReason | None = None
