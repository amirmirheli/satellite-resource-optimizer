"""Enumerations shared across the simulation.

All enums are :class:`enum.Enum` (or :class:`enum.IntEnum` where an ordering is
meaningful, e.g. priority) so they are hashable, comparable, and serialize cleanly
into telemetry.
"""

from __future__ import annotations

from enum import Enum, IntEnum


class TrafficClass(IntEnum):
    """Service classes on the roadmap, ordered by scheduling priority (higher = more urgent).

    The integer ordering is used as a coarse priority tie-breaker; finer priority,
    fairness, and degradability behavior is configured per class (see config).
    """

    THIRD_PARTY_API = 0  # variable size, lowest priority, strictly best-effort
    PHOTO = 1  # large, fully degradable, deferrable/backpressurable
    MAPS_TILES = 2  # medium/large, bandwidth-heavy, fully degradable
    FIND_MY = 3  # tiny, delay-tolerant location updates
    MESSAGING = 4  # small/medium, best-effort-but-timely
    ROADSIDE = 5  # small, high priority, near-real-time
    EMERGENCY_SOS = 6  # tiny payload, life-safety, highest priority


# Traffic classes whose requests are eligible for the Tier-2 reactive emergency lane.
URGENT_CLASSES: frozenset[TrafficClass] = frozenset(
    {TrafficClass.EMERGENCY_SOS, TrafficClass.ROADSIDE}
)


class FleetId(Enum):
    """The two heterogeneous constellations the scheduler allocates across."""

    LEGACY_LEO = "legacy_leo"  # fewer sats, narrower beams, lower aggregate capacity
    NEXT_GEN = "next_gen"  # more sats/beams, higher capacity, different geometry


class Band(Enum):
    """Radio frequency bands. Legality varies by region (see RegulatoryPolicy)."""

    L = "L"
    S = "S"
    KU = "Ku"
    KA = "Ka"


class Region(Enum):
    """Coarse regulatory regions with distinct spectrum rules.

    A real system would key off licensing geography; these stand in for that.
    """

    NA = "na"  # North America
    EU = "eu"  # Europe
    APAC = "apac"  # Asia-Pacific
    LATAM = "latam"  # Latin America
    OCEAN = "ocean"  # international waters / open ocean


class Outcome(Enum):
    """Terminal-or-pending disposition of a request after a step."""

    SERVED = "served"  # allocated resources and delivered this step
    DEFERRED = "deferred"  # re-queued for a later step (retry budget permitting)
    DROPPED = "dropped"  # abandoned (retry budget exhausted or deadline missed)
    REJECTED = "rejected"  # shed by admission control before scheduling


class RejectReason(Enum):
    """Why a request was rejected/shed, for observability."""

    ADMISSION_SHED = "admission_shed"  # probabilistic load shedding
    EMERGENCY_LANE_FULL = "emergency_lane_full"  # urgent, but reserved capacity exhausted
    NO_LEGAL_OPTION = "no_legal_option"  # no (fleet, band) legal in the request's region
    NO_CAPACITY = "no_capacity"  # scheduler found no allocatable resource
    DEADLINE_MISSED = "deadline_missed"  # could not be served before its deadline
    RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"  # too many failed attempts


class OptimizerBackend(Enum):
    """Which Tier-3 optimizer implementation to use."""

    SOLVER = "solver"  # OR-Tools MILP (primary)
    HEURISTIC = "heuristic"  # deterministic fallback / fast-test default
    ADAPTIVE = "adaptive"  # learns the admission curve online from realized utilization


class SchedulerKind(Enum):
    """Which scheduler implementation to use as the primary."""

    HEURISTIC = "heuristic"  # priority + weighted-fair
    PRIORITY_FAIR = "priority_fair"  # priority-aware constrained fairness
    SLOT_MAC = "slot_mac"  # MAC-style slot x subchannel grid with MCS link adaptation


class DegradeMode(Enum):
    """Operating mode of the control loop under resilience pressure."""

    NORMAL = "normal"  # primary scheduler
    FALLBACK = "fallback"  # fallback heuristic scheduler engaged
    SAFE = "safe"  # emergency safe mode: serve only life-safety traffic
