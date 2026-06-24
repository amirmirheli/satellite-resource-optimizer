"""Port: resource scheduler (core allocation).

Given admitted, region-filtered candidates and the merged constellation snapshot, the scheduler
allocates beam resources using priority, waiting time, fairness, expected cost, and delivery
probability. Implementations operate at different abstraction levels behind this one port:

* ``HeuristicScheduler`` / ``PriorityFairScheduler`` — fluid model: a beam is a scalar airtime
  capacity, an allocation is a number of airtime units (the default control-plane altitude).
* ``SlotMacScheduler`` — granted MAC model: a beam is a slot x subchannel resource-block grid and
  allocation is collision-free RB assignment with MCS-based link adaptation.
* ``ContentionMacScheduler`` — random-access MAC model: UEs contend for RBs on the same grid
  (slotted ALOHA), so collisions defer the colliders, with optional spreading-code capture.

The MAC schedulers map RBs back to airtime units so results stay comparable. This is intentionally
a resource-management abstraction, not a PHY/MAC simulator: there is no SINR/interference, no
per-symbol modeling, and (in the fluid schedulers) no slot/subchannel structure. ``Band`` is a
legality tag, not an allocatable bandwidth.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from satsim.domain.models import ConstellationSnapshot, ScheduleCandidate, SchedulingResult


@runtime_checkable
class ResourceScheduler(Protocol):
    """Allocates beam resources to candidate requests for one step."""

    def schedule(
        self,
        candidates: Sequence[ScheduleCandidate],
        snapshot: ConstellationSnapshot,
        step: int,
    ) -> SchedulingResult:
        """Allocate resources for ``step``.

        Inputs are already admission-shaped and regulatory-filtered (each candidate
        carries its legal options + :class:`RequestScore`). Returns served/deferred/
        dropped dispositions, the granted allocations, and fairness/utilization indicators.

        Capacity invariant: ``snapshot`` reflects capacity **net of any Tier-2 emergency
        reservations** for this step. The control loop subtracts the emergency lane's
        reservations from the merged snapshot before calling the scheduler, so the
        scheduler may treat every beam's ``capacity_units`` as freely allocatable.
        """
        ...
