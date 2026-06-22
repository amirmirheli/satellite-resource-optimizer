"""Ports: admission control.

Two ports live here because they are two faces of the same concern — deciding *which*
offered load to accept under scarcity — at two tiers:

* :class:`EmergencyAdmission` (Tier 2) runs **first**: the in-step preemptive lane for
  urgent traffic, admitting life-safety requests into bounded reserved per-beam capacity
  via its own emergency-class admission control so a surge can't starve the system.
* :class:`AdmissionController` (Tier 1) then shapes best-effort offered load
  probabilistically, using the score -> admit-probability curve broadcast by the Tier-3
  optimizer.

Urgent flow is **Tier 2 first, Tier 1 spillover**: urgent requests the emergency lane
sheds (lane full / fairness cap) fall through to Tier-1 admission as ordinary scored
load — there is no separate emergency carve-out inside :class:`AdmissionController`. The
single priority guarantee lives in Tier 2; Tier 1 just sees a high score for those
spillover requests.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from satsim.domain.models import (
    AdmissionResult,
    CongestionState,
    ConstellationSnapshot,
    ScoredRequest,
    ServiceRequest,
)
from satsim.domain.planning import EmergencyDecision, ResourcePlan


@runtime_checkable
class AdmissionController(Protocol):
    """Probabilistic load shaping applied before the scheduler (best-effort traffic)."""

    def admit(
        self,
        scored: Sequence[ScoredRequest],
        congestion: CongestionState,
        step: int,
    ) -> AdmissionResult:
        """Shed offered load: each request's precomputed score maps to an admit probability.

        Requests arrive already scored (single source of truth — no rescoring here).
        Operates on best-effort load plus any urgent requests that spilled over from the
        Tier-2 lane (which simply carry a high score). Returns the admitted requests
        (still scored) and the rejected ones (each with a ``RejectReason``).
        """
        ...

    def update_policy(self, plan: ResourcePlan) -> None:
        """Adopt the latest Tier-3 plan (its broadcast score -> admit-probability curve)."""
        ...


@runtime_checkable
class EmergencyAdmission(Protocol):
    """Tier-2 reactive lane: admit urgent traffic into bounded reserved capacity."""

    def reserve(
        self,
        urgent: Sequence[ServiceRequest],
        snapshot: ConstellationSnapshot,
        plan: ResourcePlan,
        step: int,
    ) -> EmergencyDecision:
        """Run emergency-class admission control over the urgent requests.

        Scores urgent requests on severity, wait time, retry count, signal quality, and
        geographic fairness, then admits as many as fit within bounded per-beam
        reservation caps. Requests that don't fit are shed (to fall through to the
        best-effort path or be queued), so a mass-casualty surge prioritizes life-safety
        without monopolizing a beam or starving the system.
        """
        ...
