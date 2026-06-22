"""Default :class:`~satsim.ports.admission.AdmissionController`: probabilistic shedding.

Each request carries a precomputed score; the current Tier-3 plan's admission curve maps
that score to an admit probability, and a seeded coin flip decides. Between Tier-3 updates
the controller also reacts to *live* congestion: when offered load exceeds capacity it sheds
extra load, but weighted by ``(1 - score)`` so high-priority/urgent traffic is barely
touched while low-priority best-effort traffic absorbs the backpressure.
"""

from __future__ import annotations

from collections.abc import Sequence

from satsim.domain.enums import RejectReason
from satsim.domain.models import (
    AdmissionResult,
    CongestionState,
    RejectedRequest,
    ScoredRequest,
)
from satsim.domain.planning import ResourcePlan
from satsim.rng import Rng


class ProbabilisticAdmissionController:
    """Sheds offered load via a score -> admit-probability curve + live congestion damping."""

    def __init__(self, rng: Rng, plan: ResourcePlan | None = None) -> None:
        self._rng = rng
        self._plan = plan if plan is not None else ResourcePlan.passthrough()

    def update_policy(self, plan: ResourcePlan) -> None:
        """Adopt the latest Tier-3 plan (its broadcast score -> admit-probability curve)."""
        self._plan = plan

    def admit(
        self,
        scored: Sequence[ScoredRequest],
        congestion: CongestionState,
        step: int,
    ) -> AdmissionResult:
        curve = self._plan.admission_curve
        # Extra shedding pressure when over-subscribed or when consumer lag is building.
        overage = min(1.0, max(0.0, congestion.utilization - 1.0) + congestion.queue_pressure)

        admitted: list[ScoredRequest] = []
        rejected: list[RejectedRequest] = []
        for item in scored:
            score = item.scoring.score
            prob = curve.admit_probability(score)
            # Damp low-score traffic harder than high-score traffic under congestion.
            prob *= 1.0 - overage * (1.0 - score)
            if self._rng.random() < prob:
                admitted.append(item)
            else:
                rejected.append(RejectedRequest(item.request, RejectReason.ADMISSION_SHED))

        return AdmissionResult(admitted=tuple(admitted), rejected=tuple(rejected))
