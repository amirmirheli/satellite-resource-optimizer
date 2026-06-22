"""Typed, validated simulation configuration (pydantic v2).

Every knob the simulation exposes lives here: arrival/demand model, per-class traffic
profiles, per-fleet capacities, per-region spectrum rules, overload thresholds, the
Tier-3 optimizer cadence/backend, and the Tier-2 emergency-lane caps. Models are frozen
and reject unknown fields so misconfiguration fails loudly and runs stay reproducible.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from satsim.domain.enums import (
    Band,
    FleetId,
    OptimizerBackend,
    Region,
    SchedulerKind,
    TrafficClass,
)


class _Base(BaseModel):
    """Shared base: immutable, strict (no extra/unknown fields), validate on assignment."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_assignment=True)


class ClassProfile(_Base):
    """Demand profile for a single traffic class."""

    weight: float = Field(ge=0.0, description="Relative arrival weight within the mix.")
    min_size_bytes: int = Field(gt=0)
    max_size_bytes: int = Field(gt=0)
    deadline_slack_steps: int = Field(ge=0, description="Steps after arrival before expiry.")
    retry_budget: int = Field(ge=0, description="Max deferral attempts before drop.")
    degradable: bool = Field(default=False, description="May be backpressured/degraded.")

    @model_validator(mode="after")
    def _check_sizes(self) -> ClassProfile:
        if self.max_size_bytes < self.min_size_bytes:
            raise ValueError("max_size_bytes must be >= min_size_bytes")
        return self


class ArrivalConfig(_Base):
    """Baseline arrival process (Poisson) plus the per-step ingest batch cap."""

    baseline_rate: float = Field(ge=0.0, description="Mean baseline arrivals per step (lambda).")
    max_batch_per_step: int = Field(gt=0, description="Max requests drained from the bus/step.")
    link_quality_min: float = Field(
        default=0.1, gt=0.0, le=1.0, description="Lower bound on sampled link quality."
    )
    link_quality_max: float = Field(
        default=1.0, gt=0.0, le=1.0, description="Upper bound on sampled link quality."
    )
    surge_urgency_jitter: float = Field(
        default=0.2, ge=0.0, description="+/- spread applied to a surge's mean urgency."
    )

    @model_validator(mode="after")
    def _check_link_range(self) -> ArrivalConfig:
        if self.link_quality_max < self.link_quality_min:
            raise ValueError("link_quality_max must be >= link_quality_min")
        return self


class SurgeEvent(_Base):
    """A scheduled burst injected on top of baseline arrivals (e.g. emergency surge)."""

    at_step: int = Field(ge=0)
    count: int = Field(gt=0)
    traffic_class: TrafficClass = TrafficClass.EMERGENCY_SOS
    region: Region = Region.NA
    urgency_mean: float = Field(default=0.9, ge=0.0, le=1.0)


class FleetConfig(_Base):
    """Capacity shape of one constellation fleet."""

    fleet: FleetId
    num_satellites: int = Field(ge=0)
    beams_per_satellite: int = Field(ge=0)
    capacity_per_beam: float = Field(ge=0.0, description="Allocatable airtime units per beam/step.")
    online_at_step: int = Field(default=0, ge=0, description="Step the fleet becomes available.")
    fail_from_step: int | None = Field(
        default=None, ge=0, description="Step from which this fleet's client starts failing."
    )


class RegionRule(_Base):
    """Spectrum legality + caps for one region."""

    region: Region
    allowed_bands: tuple[Band, ...]
    allowed_fleets: tuple[FleetId, ...]
    max_airtime_units: float | None = Field(default=None, ge=0.0)
    max_power: float | None = Field(default=None, ge=0.0)


class OverloadConfig(_Base):
    """Bounded-queue, congestion, and resilience thresholds driving backpressure/shedding."""

    queue_capacity: int = Field(gt=0, description="Max backlog retained across steps.")
    history_capacity: int = Field(default=256, gt=0, description="Bounded telemetry history.")
    collapse_utilization: float = Field(
        default=1.5, gt=0.0, description="Utilization above which collapse risk is high."
    )

    # Circuit breaker (per constellation): trip after N consecutive failures, retry after cooldown.
    breaker_failure_threshold: int = Field(default=3, gt=0)
    breaker_cooldown_steps: int = Field(default=10, gt=0)

    # Retry backoff (exponential + jitter) to avoid retransmission storms.
    retry_backoff_base_steps: int = Field(default=1, ge=0)
    retry_backoff_max_steps: int = Field(default=16, gt=0)


class ScoringConfig(_Base):
    """Tunables for the request scorer (composite score weights + airtime cost model)."""

    priority_weight: float = Field(default=0.6, ge=0.0, description="Weight on class priority.")
    urgency_weight: float = Field(default=0.25, ge=0.0, description="Weight on urgency.")
    waiting_weight: float = Field(default=0.15, ge=0.0, description="Weight on waiting pressure.")
    bytes_per_unit: float = Field(
        default=4096.0, gt=0.0, description="Bytes that fit in one airtime unit on a clean link."
    )
    min_link_quality: float = Field(
        default=0.05, gt=0.0, le=1.0, description="Cost-model floor on link quality (avoid /0)."
    )


class SchedulerConfig(_Base):
    """Tunables shared by the schedulers."""

    priority_fair_max_units_per_request: float = Field(
        default=6.0, gt=0.0, description="Per-request airtime cap for PriorityFairScheduler."
    )
    degrade_min_fraction: float = Field(
        default=0.25, gt=0.0, le=1.0,
        description="Min fraction of a degradable request's cost worth serving degraded.",
    )


class McsLevel(_Base):
    """One modulation-and-coding tier: usable above a link-quality threshold."""

    min_link_quality: float = Field(ge=0.0, le=1.0, description="Min link quality to use this MCS.")
    bits_per_symbol: float = Field(gt=0.0, description="Spectral efficiency (bits per symbol).")
    name: str = Field(default="", description="Human-readable MCS name (e.g. QPSK).")


def _default_mcs() -> tuple[McsLevel, ...]:
    """A small, monotone MCS ladder (robust → high-throughput)."""
    return (
        McsLevel(min_link_quality=0.0, bits_per_symbol=1.0, name="BPSK"),
        McsLevel(min_link_quality=0.2, bits_per_symbol=2.0, name="QPSK"),
        McsLevel(min_link_quality=0.4, bits_per_symbol=4.0, name="16QAM"),
        McsLevel(min_link_quality=0.6, bits_per_symbol=6.0, name="64QAM"),
        McsLevel(min_link_quality=0.8, bits_per_symbol=8.0, name="256QAM"),
    )


class MacConfig(_Base):
    """MAC-layer grid for the SlotMacScheduler: a beam's capacity becomes a slot x subchannel grid.

    Each resource block (RB) is one (slot, subchannel) cell. How many RBs a request needs depends
    on its payload and link quality via the MCS ladder (better link → higher MCS → more bits/RB →
    fewer RBs), so this models real link adaptation rather than fluid airtime.
    """

    slots_per_step: int = Field(default=10, gt=0, description="Time slots per step per beam.")
    subchannels: int = Field(default=4, gt=0, description="Frequency subchannels per beam.")
    symbols_per_rb: int = Field(default=2048, gt=0, description="Symbols carried by one RB.")
    max_rbs_per_request: int | None = Field(
        default=None, ge=1, description="Optional per-request RB cap (MAC fairness)."
    )
    degrade_min_fraction: float = Field(
        default=0.25, gt=0.0, le=1.0, description="Min fraction of needed RBs worth serving."
    )
    mcs_table: tuple[McsLevel, ...] = Field(default_factory=_default_mcs)

    @model_validator(mode="after")
    def _check_mcs(self) -> MacConfig:
        if not self.mcs_table:
            raise ValueError("mcs_table must have at least one MCS level")
        return self

    @property
    def resource_blocks_per_beam(self) -> int:
        return self.slots_per_step * self.subchannels


class OptimizerConfig(_Base):
    """Tier-3 periodic optimizer cadence + backend selection."""

    backend: OptimizerBackend = OptimizerBackend.HEURISTIC
    cadence_steps: int = Field(default=10, gt=0, description="Run the optimizer every N steps.")
    utilization_clamp: float = Field(
        default=10.0, gt=0.0, description="Cap on utilization before averaging (can be +inf)."
    )
    fairness_weight_min: float = Field(default=0.5, gt=0.0, description="Min fairness weight.")
    fairness_weight_max: float = Field(default=1.5, gt=0.0, description="Max fairness weight.")
    solver_time_limit_s: float = Field(
        default=0.5, gt=0.0, description="MILP time limit; on timeout, heuristic fallback."
    )

    # Solver MILP objective weights (relative; all terms are roughly capacity-scaled).
    served_weight: float = Field(default=1.0, ge=0.0, description="Reward per served unit.")
    fairness_bonus_weight: float = Field(
        default=0.3, ge=0.0, description="Bonus for serving under-served classes."
    )
    assignment_weight: float = Field(
        default=0.2, ge=0.0, description="Weight on demand-weighted fleet assignment."
    )
    switching_penalty_weight: float = Field(
        default=0.5, ge=0.0, description="Penalty per region whose fleet changes."
    )
    congestion_penalty_weight: float = Field(
        default=0.1, ge=0.0, description="Penalty per airtime unit above the soft threshold."
    )
    soft_utilization: float = Field(
        default=0.9, gt=0.0, le=1.0, description="Per-region utilization where congestion starts."
    )

    # Adaptive (learning) admission curve — only used when backend is ADAPTIVE.
    adaptive_buckets: int = Field(
        default=8, gt=0, description="Score buckets in the learned admission curve."
    )
    adaptive_learning_rate: float = Field(
        default=0.3, gt=0.0, le=1.0, description="Per-cadence step size of the online update."
    )
    adaptive_signal_smoothing: float = Field(
        default=0.5, gt=0.0, le=1.0,
        description="EWMA weight on each window's realized-utilization feedback.",
    )
    adaptive_floor_min: float = Field(
        default=0.05, ge=0.0, le=1.0, description="Lower bound on any bucket's admit probability."
    )
    adaptive_collapse_penalty: float = Field(
        default=1.0, ge=0.0, description="Downward pressure per unit of congestion-collapse risk."
    )


class EmergencyConfig(_Base):
    """Tier-2 reactive emergency-lane reservation + fairness caps."""

    reserved_fraction: float = Field(
        default=0.2, ge=0.0, le=1.0, description="Fraction of each beam reserved for urgent."
    )
    max_units_per_request: float = Field(
        default=4.0, gt=0.0, description="Per-request airtime cap in the lane (airtime fairness)."
    )
    geo_fairness_weight: float = Field(
        default=1.0, ge=0.0, description="Weight on geographic fairness in lane scoring."
    )
    max_retries: int = Field(default=8, ge=0, description="Retry budget for urgent requests.")

    # Emergency-ranking weights (severity dominates, then waiting, then retries).
    severity_weight: float = Field(default=0.5, ge=0.0)
    waiting_weight: float = Field(default=0.3, ge=0.0)
    retry_weight: float = Field(default=0.2, ge=0.0)


def _default_class_mix() -> dict[TrafficClass, ClassProfile]:
    """A sensible default roadmap mix; sizes in bytes, deadlines in steps."""
    return {
        TrafficClass.EMERGENCY_SOS: ClassProfile(
            weight=0.05, min_size_bytes=64, max_size_bytes=256,
            deadline_slack_steps=20, retry_budget=8, degradable=False,
        ),
        TrafficClass.ROADSIDE: ClassProfile(
            weight=0.05, min_size_bytes=128, max_size_bytes=512,
            deadline_slack_steps=8, retry_budget=4, degradable=False,
        ),
        TrafficClass.MESSAGING: ClassProfile(
            weight=0.30, min_size_bytes=256, max_size_bytes=4_096,
            deadline_slack_steps=6, retry_budget=3, degradable=False,
        ),
        TrafficClass.FIND_MY: ClassProfile(
            weight=0.15, min_size_bytes=32, max_size_bytes=128,
            deadline_slack_steps=30, retry_budget=5, degradable=True,
        ),
        TrafficClass.MAPS_TILES: ClassProfile(
            weight=0.20, min_size_bytes=8_192, max_size_bytes=65_536,
            deadline_slack_steps=12, retry_budget=2, degradable=True,
        ),
        TrafficClass.PHOTO: ClassProfile(
            weight=0.15, min_size_bytes=65_536, max_size_bytes=1_048_576,
            deadline_slack_steps=40, retry_budget=2, degradable=True,
        ),
        TrafficClass.THIRD_PARTY_API: ClassProfile(
            weight=0.10, min_size_bytes=128, max_size_bytes=16_384,
            deadline_slack_steps=10, retry_budget=1, degradable=True,
        ),
    }


def _default_fleets() -> tuple[FleetConfig, ...]:
    return (
        FleetConfig(
            fleet=FleetId.LEGACY_LEO, num_satellites=4, beams_per_satellite=3,
            capacity_per_beam=10.0, online_at_step=0,
        ),
        FleetConfig(
            fleet=FleetId.NEXT_GEN, num_satellites=8, beams_per_satellite=6,
            capacity_per_beam=16.0, online_at_step=50,  # capacity expansion mid-run
        ),
    )


def _default_region_rules() -> tuple[RegionRule, ...]:
    both = (FleetId.LEGACY_LEO, FleetId.NEXT_GEN)
    return (
        RegionRule(region=Region.NA, allowed_bands=(Band.L, Band.S, Band.KU), allowed_fleets=both),
        RegionRule(region=Region.EU, allowed_bands=(Band.L, Band.S), allowed_fleets=both),
        RegionRule(
            region=Region.APAC, allowed_bands=(Band.S,), allowed_fleets=(FleetId.LEGACY_LEO,),
            max_airtime_units=8.0,
        ),
        RegionRule(region=Region.LATAM, allowed_bands=(Band.L, Band.KU), allowed_fleets=both),
        RegionRule(
            region=Region.OCEAN, allowed_bands=(Band.L,), allowed_fleets=(FleetId.NEXT_GEN,)
        ),
    )


class SimulationConfig(_Base):
    """Top-level configuration for a simulation run."""

    seed: int = Field(default=0, description="RNG seed for reproducibility.")
    duration_steps: int = Field(default=200, gt=0)
    time_step_seconds: float = Field(default=1.0, gt=0.0)

    arrival: ArrivalConfig = ArrivalConfig(baseline_rate=20.0, max_batch_per_step=512)
    class_mix: dict[TrafficClass, ClassProfile] = Field(default_factory=_default_class_mix)
    surges: tuple[SurgeEvent, ...] = ()

    fleets: tuple[FleetConfig, ...] = Field(default_factory=_default_fleets)
    region_rules: tuple[RegionRule, ...] = Field(default_factory=_default_region_rules)

    overload: OverloadConfig = OverloadConfig(queue_capacity=4096)
    optimizer: OptimizerConfig = OptimizerConfig()
    emergency: EmergencyConfig = EmergencyConfig()
    scoring: ScoringConfig = ScoringConfig()
    scheduler_params: SchedulerConfig = SchedulerConfig()
    mac: MacConfig = MacConfig()
    scheduler: SchedulerKind = SchedulerKind.PRIORITY_FAIR

    @model_validator(mode="after")
    def _validate(self) -> SimulationConfig:
        if not self.class_mix:
            raise ValueError("class_mix must define at least one traffic class")
        if sum(p.weight for p in self.class_mix.values()) <= 0.0:
            raise ValueError("class_mix weights must sum to a positive value")
        if not self.fleets:
            raise ValueError("at least one fleet must be configured")
        rule_regions = [r.region for r in self.region_rules]
        if len(rule_regions) != len(set(rule_regions)):
            raise ValueError("duplicate region in region_rules")
        for surge in self.surges:
            if surge.at_step >= self.duration_steps:
                raise ValueError(f"surge at_step {surge.at_step} is beyond duration_steps")
        return self
