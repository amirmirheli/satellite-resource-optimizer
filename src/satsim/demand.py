"""Demand generator — synthesizes service requests (the producer side).

Draws baseline arrivals from a Poisson process and overlays scheduled surge events (per
:class:`satsim.config.SimulationConfig`), sampling each request's class, region, size,
link quality, urgency, and deadline from the configured profiles using the seeded
:class:`satsim.rng.Rng`, then publishes onto the :class:`satsim.bus.InMemoryBus`.
"""

from __future__ import annotations

from satsim.bus import InMemoryBus
from satsim.config import ClassProfile, SimulationConfig
from satsim.domain.enums import Region, TrafficClass
from satsim.domain.models import ServiceRequest
from satsim.rng import Rng


class DemandGenerator:
    """Produces and publishes service requests for each step."""

    def __init__(self, config: SimulationConfig, rng: Rng, bus: InMemoryBus) -> None:
        self._config = config
        self._rng = rng
        self._bus = bus
        self._seq = 0  # global request counter for unique, stable ids
        # Precompute weighted class list and the region list once.
        self._class_weights: list[tuple[float, TrafficClass]] = [
            (profile.weight, tc) for tc, profile in config.class_mix.items()
        ]
        self._regions: list[Region] = [rule.region for rule in config.region_rules]

    def generate_step(self, step: int) -> int:
        """Generate and publish this step's requests; return how many were produced."""
        produced = 0

        # Baseline Poisson arrivals.
        for _ in range(self._rng.poisson(self._config.arrival.baseline_rate)):
            tc = self._rng.weighted_choice(self._class_weights)
            region = self._regions[self._rng.randint(0, len(self._regions) - 1)]
            self._publish(step, tc, region, urgency=self._rng.random())
            produced += 1

        # Scheduled surge events for this step.
        for surge in self._config.surges:
            if surge.at_step != step:
                continue
            jitter = self._config.arrival.surge_urgency_jitter
            for _ in range(surge.count):
                urgency = _clamp(surge.urgency_mean + (self._rng.random() - 0.5) * jitter)
                self._publish(step, surge.traffic_class, surge.region, urgency=urgency)
                produced += 1

        return produced

    def _publish(self, step: int, tc: TrafficClass, region: Region, urgency: float) -> None:
        profile = self._config.class_mix[tc]
        arrival = self._config.arrival
        link_floor = arrival.link_quality_min
        link_span = max(0.0, arrival.link_quality_max - link_floor)
        request = ServiceRequest(
            request_id=f"req-{step}-{self._seq}",
            traffic_class=tc,
            region=region,
            size_bytes=self._sample_size(profile),
            link_quality=link_floor + link_span * self._rng.random(),
            urgency=urgency,
            arrival_step=step,
            deadline_step=step + profile.deadline_slack_steps,
        )
        self._seq += 1
        self._bus.publish(request, available_at_step=step)

    def _sample_size(self, profile: ClassProfile) -> int:
        return self._rng.randint(profile.min_size_bytes, profile.max_size_bytes)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
