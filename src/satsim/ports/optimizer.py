"""Port: Tier-3 periodic global optimizer.

Runs on a slower cadence than the per-step loop to do planning the loop can't afford
every tick: recompute the admission score -> probability curve, fairness weights /
airtime budgets, and fleet/beam assignment hints. The primary implementation is a
MILP solved with OR-Tools (``SolverOptimizer``); a deterministic ``HeuristicOptimizer``
sits behind this same port as the fallback (on solver timeout) and as the default in
fast tests.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from satsim.domain.planning import PlanningWindow, ResourcePlan


@runtime_checkable
class Optimizer(Protocol):
    """Produces a :class:`ResourcePlan` from a global :class:`PlanningWindow`."""

    def plan(self, window: PlanningWindow) -> ResourcePlan:
        """Compute the next resource plan.

        Implementations should be effectively deterministic for a given window (the
        solver runs under a time limit and falls back to the heuristic if it cannot
        prove a solution in time), so simulation runs remain reproducible.
        """
        ...
