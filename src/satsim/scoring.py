"""Request scoring — the single source of truth for a request's score, cost, and
delivery probability.

Deliberately *not* a port: scoring is deterministic internal logic with no external
dependency. The control loop calls it once per request per step, and the resulting
:class:`~satsim.domain.models.RequestScore` feeds three consumers that must agree —
the admission curve (score -> admit probability), the congestion estimate (offered
load = sum of estimated costs), and the scheduler (ordering + delivery probability).

The composite ``score`` combines class priority, within-class urgency, and waiting-time
pressure (how close the request is to its deadline), then applies the current Tier-3
plan's per-class fairness weight. ``estimated_cost_units`` scales payload size by link
quality (a poor link costs more airtime to clear), and ``delivery_probability`` tracks
link quality directly.
"""

from __future__ import annotations

from satsim.config import SimulationConfig
from satsim.domain.enums import TrafficClass
from satsim.domain.models import RequestScore, ServiceRequest
from satsim.domain.planning import ResourcePlan

# Highest TrafficClass value, used to normalize class priority into [0, 1].
_MAX_PRIORITY = max(tc.value for tc in TrafficClass)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def estimate_cost_units(
    size_bytes: int, link_quality: float, bytes_per_unit: float, min_link: float
) -> float:
    """Expected airtime units to clear ``size_bytes`` over a link of the given quality.

    A poorer link costs more airtime (more retransmissions). Shared by the scorer and the
    Tier-2 emergency lane so both price airtime identically.
    """
    link = max(min_link, link_quality)
    return (size_bytes / bytes_per_unit) / link


class RequestScorer:
    """Computes the authoritative :class:`RequestScore` for a request."""

    def __init__(self, config: SimulationConfig) -> None:
        self._config = config
        self._scoring = config.scoring

    def evaluate(
        self, request: ServiceRequest, current_step: int, plan: ResourcePlan
    ) -> RequestScore:
        """Score ``request`` at ``current_step`` under the current Tier-3 ``plan``."""
        cfg = self._scoring
        priority = request.traffic_class.value / _MAX_PRIORITY
        urgency = _clamp(request.urgency)

        span = max(1, request.deadline_step - request.arrival_step)
        waiting = _clamp(request.waiting_steps(current_step) / span)

        base = (
            cfg.priority_weight * priority
            + cfg.urgency_weight * urgency
            + cfg.waiting_weight * waiting
        )
        fairness = plan.fairness_weights.get(request.traffic_class, 1.0)
        score = _clamp(base * fairness)

        cost = estimate_cost_units(
            request.size_bytes, request.link_quality, cfg.bytes_per_unit, cfg.min_link_quality
        )
        return RequestScore(
            score=score,
            estimated_cost_units=cost,
            delivery_probability=_clamp(request.link_quality),
        )
