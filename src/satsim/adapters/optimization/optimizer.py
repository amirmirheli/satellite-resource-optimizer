"""Default :class:`~satsim.ports.optimizer.Optimizer` implementations (Tier 3).

Two implementations behind one port:

* :class:`HeuristicOptimizer` — closed-form, deterministic, zero-dependency. The default on
  the fast test path and the fallback when the solver can't finish in time.
* :class:`SolverOptimizer` — an OR-Tools MILP that jointly (a) assigns each region a primary
  fleet under a per-fleet load-balancing cap (the integer part) and (b) allocates a per-class
  airtime budget maximizing priority-weighted served demand (the continuous part). On any
  failure or timeout it delegates to a :class:`HeuristicOptimizer`.

Both share the same congestion -> admission-curve logic, so the only thing that differs is
how fairness weights, airtime budgets, and fleet hints are derived.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from satsim.config import SimulationConfig
from satsim.domain.enums import FleetId, OptimizerBackend, Region, RejectReason, TrafficClass
from satsim.domain.models import ConstellationSnapshot
from satsim.domain.planning import AdmissionCurve, PlanningWindow, ResourcePlan
from satsim.ports.optimizer import Optimizer

_MAX_PRIORITY = max(tc.value for tc in TrafficClass)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- shared signals


def _mean_utilization(window: PlanningWindow, clamp: float) -> float:
    if window.recent_congestion:
        utils = [min(clamp, c.utilization) for c in window.recent_congestion]
        return sum(utils) / len(utils)
    total_demand = sum(window.recent_demand_units.values())
    capacity = window.snapshot.total_capacity()
    if capacity > 0.0:
        return min(clamp, total_demand / capacity)
    return 0.0


def _mean_drop_rate(window: PlanningWindow) -> float:
    if not window.recent_congestion:
        return 0.0
    return sum(c.drop_rate for c in window.recent_congestion) / len(window.recent_congestion)


def _admission_curve(util: float, drop_rate: float) -> AdmissionCurve:
    """Monotone score->prob curve that sheds *proportionally* rather than collapsing to zero.

    The score-0 floor tracks ~1/util — i.e. admit roughly the fraction of low-value load that
    capacity can actually serve — so beams stay utilized instead of going idle under bursty,
    heavy (e.g. PHOTO) demand. High-score traffic is always admitted; a high drop rate lowers
    the floor further.
    """
    floor = 1.0 if util <= 1.0 else 1.0 / util
    floor = _clamp(floor * (1.0 - drop_rate))
    return AdmissionCurve(breakpoints=((0.0, floor), (1.0, 1.0)))


def _demand_shares(window: PlanningWindow) -> dict[TrafficClass, float]:
    demand = window.recent_demand_units
    total = sum(demand.values())
    if total <= 0.0:
        return {}
    return {tc: d / total for tc, d in demand.items() if d > 0.0}


def _served_by_class(window: PlanningWindow) -> dict[TrafficClass, int]:
    served: dict[TrafficClass, int] = {}
    for counters in window.recent_counters:
        for tc, n in counters.served_by_class.items():
            served[tc] = served.get(tc, 0) + n
    return served


def _capacity_by_region_fleet(
    snapshot: ConstellationSnapshot,
) -> dict[Region, dict[FleetId, float]]:
    table: dict[Region, dict[FleetId, float]] = {}
    for beam in snapshot.beams:
        per_fleet = table.setdefault(beam.region, {})
        per_fleet[beam.fleet] = per_fleet.get(beam.fleet, 0.0) + beam.capacity_units
    return table


def _fairness_weights(
    window: PlanningWindow, lo: float, hi: float
) -> dict[TrafficClass, float]:
    """Boost classes served less than their demand share; damp those served more (in [lo, hi])."""
    shares = _demand_shares(window)
    if not shares:
        return {}
    served = _served_by_class(window)
    served_total = sum(served.values())
    weights: dict[TrafficClass, float] = {}
    for tc, demand_share in shares.items():
        served_share = served.get(tc, 0) / served_total if served_total > 0 else 0.0
        ratio = demand_share / max(served_share, 1e-6)
        weights[tc] = _clamp(ratio, lo, hi)
    return weights


# --------------------------------------------------------------------------- heuristic


class HeuristicOptimizer:
    """Closed-form, deterministic Tier-3 planner (also the solver's fallback)."""

    def __init__(
        self,
        valid_for_steps: int = 1,
        *,
        util_clamp: float = 10.0,
        fairness_min: float = 0.5,
        fairness_max: float = 1.5,
    ) -> None:
        self._valid_for_steps = valid_for_steps
        self._util_clamp = util_clamp
        self._fairness_min = fairness_min
        self._fairness_max = fairness_max

    def plan(self, window: PlanningWindow) -> ResourcePlan:
        util = _mean_utilization(window, self._util_clamp)
        curve = _admission_curve(util, _mean_drop_rate(window))

        capacity = window.snapshot.total_capacity()
        shares = _demand_shares(window)
        budgets = {tc: capacity * share for tc, share in shares.items()}

        hints = {
            region: max(per_fleet.items(), key=lambda kv: (kv[1], kv[0].value))[0]
            for region, per_fleet in _capacity_by_region_fleet(window.snapshot).items()
            if per_fleet
        }

        return ResourcePlan(
            generated_at_step=window.step,
            valid_for_steps=self._valid_for_steps,
            admission_curve=curve,
            fairness_weights=_fairness_weights(window, self._fairness_min, self._fairness_max),
            airtime_budgets=budgets,
            fleet_hints=hints,
        )


# --------------------------------------------------------------------------- solver


def _fairness_bonus(window: PlanningWindow) -> dict[TrafficClass, float]:
    """Objective bonus per class: positive when its recent served share trails its demand share."""
    shares = _demand_shares(window)
    if not shares:
        return {}
    served = _served_by_class(window)
    served_total = sum(served.values())
    out: dict[TrafficClass, float] = {}
    for tc, demand_share in shares.items():
        served_share = served.get(tc, 0) / served_total if served_total > 0 else 0.0
        out[tc] = _clamp(demand_share - served_share)
    return out


class SolverOptimizer:
    """OR-Tools MILP Tier-3 planner; delegates to the heuristic on timeout/failure.

    Jointly optimizes a binary region->fleet assignment and continuous per-(region, class)
    airtime budgets, *coupled* so a region's budgets are bounded by its assigned fleet's
    capacity. The objective rewards priority-weighted served airtime (which simultaneously
    minimizes priority-weighted drops — drop = demand - served), adds a fairness bonus for
    under-served classes and a demand-pressure-weighted assignment term, and penalizes
    congestion (airtime above a soft utilization threshold) and fleet switching between runs.
    """

    def __init__(
        self,
        valid_for_steps: int = 1,
        time_limit_s: float = 0.5,
        *,
        util_clamp: float = 10.0,
        fairness_min: float = 0.5,
        fairness_max: float = 1.5,
        served_weight: float = 1.0,
        fairness_bonus_weight: float = 0.3,
        assignment_weight: float = 0.2,
        switching_penalty_weight: float = 0.5,
        congestion_penalty_weight: float = 0.1,
        soft_utilization: float = 0.9,
    ) -> None:
        self._valid_for_steps = valid_for_steps
        self._time_limit_s = time_limit_s
        self._util_clamp = util_clamp
        self._fairness_min = fairness_min
        self._fairness_max = fairness_max
        self._served_weight = served_weight
        self._fairness_bonus_weight = fairness_bonus_weight
        self._assignment_weight = assignment_weight
        self._switching_penalty_weight = switching_penalty_weight
        self._congestion_penalty_weight = congestion_penalty_weight
        self._soft_utilization = soft_utilization
        self._prev_hints: dict[Region, FleetId] = {}  # for the switching penalty across runs
        self._fallback = HeuristicOptimizer(
            valid_for_steps,
            util_clamp=util_clamp,
            fairness_min=fairness_min,
            fairness_max=fairness_max,
        )

    def plan(self, window: PlanningWindow) -> ResourcePlan:
        try:
            solved = self._solve(window)
        except Exception:  # noqa: BLE001 - any solver issue must degrade, never crash
            solved = None
        return solved if solved is not None else self._fallback.plan(window)

    def _solve(self, window: PlanningWindow) -> ResourcePlan | None:
        from ortools.linear_solver import pywraplp

        cap_table = _capacity_by_region_fleet(window.snapshot)
        if window.snapshot.total_capacity() <= 0.0 or not cap_table:
            return None  # nothing to plan; let the heuristic handle the trivial case
        demand_rc = window.recent_demand_by_region

        solver = pywraplp.Solver.CreateSolver("CBC")
        if solver is None:
            return None
        solver.SetTimeLimit(int(self._time_limit_s * 1000))

        regions = sorted(cap_table.keys(), key=lambda r: r.value)
        fleets = sorted({f for pf in cap_table.values() for f in pf}, key=lambda f: f.value)
        max_per_fleet = math.ceil(len(regions) / len(fleets)) + 1

        # Binary assignment: each region gets exactly one primary fleet, balanced across fleets.
        x = {
            (r, f): solver.BoolVar(f"x_{r.value}_{f.value}")
            for r in regions
            for f in cap_table[r]
        }
        for r in regions:
            solver.Add(solver.Sum([x[r, f] for f in cap_table[r]]) == 1)
        for f in fleets:
            in_f = [x[r, f] for r in regions if f in cap_table[r]]
            if in_f:
                solver.Add(solver.Sum(in_f) <= max_per_fleet)

        # Assigned capacity available in region r (linear in x).
        cap_expr = {
            r: solver.Sum([cap_table[r][f] * x[r, f] for f in cap_table[r]]) for r in regions
        }

        # Continuous per-(region, class) budgets, bounded by demand (var ub) and assigned capacity.
        y = {
            (r, c): solver.NumVar(0.0, d, f"y_{r.value}_{c.name}")
            for (r, c), d in demand_rc.items()
            if r in cap_table and d > 0.0
        }
        region_classes: dict[Region, list[TrafficClass]] = {}
        for r, c in y:
            region_classes.setdefault(r, []).append(c)

        u: dict[Region, Any] = {}
        for r, classes in region_classes.items():
            served_r = solver.Sum([y[r, c] for c in classes])
            solver.Add(served_r <= cap_expr[r])  # coupling: budgets <= assigned capacity
            u[r] = solver.NumVar(0.0, solver.infinity(), f"u_{r.value}")
            solver.Add(u[r] >= served_r - self._soft_utilization * cap_expr[r])  # congestion slack

        # Objective.
        fair = _fairness_bonus(window)
        pressure = _region_pressure_share(demand_rc, regions)
        serve_terms = [
            (
                self._served_weight * (c.value / _MAX_PRIORITY)
                + self._fairness_bonus_weight * fair.get(c, 0.0)
            )
            * var
            for (_r, c), var in y.items()
        ]
        assign_terms = [
            self._assignment_weight * cap_table[r][f] * pressure[r] * x[r, f]
            for r in regions
            for f in cap_table[r]
        ]
        switch_terms = [
            -self._switching_penalty_weight * x[r, f]
            for r in regions
            if self._prev_hints.get(r) is not None
            for f in cap_table[r]
            if f != self._prev_hints[r]
        ]
        cong_terms = [-self._congestion_penalty_weight * uvar for uvar in u.values()]
        solver.Maximize(solver.Sum(serve_terms + assign_terms + switch_terms + cong_terms))

        status = solver.Solve()
        if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
            return None

        hints: dict[Region, FleetId] = {}
        for r in regions:
            for f in cap_table[r]:
                if x[r, f].solution_value() > 0.5:
                    hints[r] = f
                    break

        region_budgets: dict[tuple[Region, TrafficClass], float] = {}
        budgets: dict[TrafficClass, float] = {}
        for (r, c), var in y.items():
            value = var.solution_value()
            if value > 1e-9:
                region_budgets[r, c] = value
                budgets[c] = budgets.get(c, 0.0) + value

        fairness: dict[TrafficClass, float] = {}
        demand_by_class = window.recent_demand_units
        for c in {c for (_r, c) in y}:
            d = demand_by_class.get(c, 0.0)
            fill = budgets.get(c, 0.0) / d if d > 0 else 1.0
            fairness[c] = _clamp(
                self._fairness_max - 0.5 * fill, self._fairness_min, self._fairness_max
            )

        self._prev_hints = hints  # remember for the next run's switching penalty
        util = _mean_utilization(window, self._util_clamp)
        return ResourcePlan(
            generated_at_step=window.step,
            valid_for_steps=self._valid_for_steps,
            admission_curve=_admission_curve(util, _mean_drop_rate(window)),
            fairness_weights=fairness,
            airtime_budgets=budgets,
            region_class_budgets=region_budgets,
            fleet_hints=hints,
        )


def _region_pressure_share(
    demand_rc: Mapping[tuple[Region, TrafficClass], float], regions: list[Region]
) -> dict[Region, float]:
    """Each region's share of total demand (its 'demand pressure'), in [0, 1]."""
    per_region = {r: 0.0 for r in regions}
    for (r, _c), d in demand_rc.items():
        if r in per_region and d > 0.0:
            per_region[r] += d
    total = sum(per_region.values())
    if total <= 0.0:
        return {r: 0.0 for r in regions}
    return {r: per_region[r] / total for r in regions}


# --------------------------------------------------------------------------- adaptive


class AdaptiveOptimizer:
    """Online, per-score-bucket admission curve that *learns* from realized utilization.

    The heuristic and solver curves recompute a fixed formula of *offered*-load utilization
    each cadence and keep no memory between runs. This optimizer instead holds the admit
    probability of each score bucket as persistent state and nudges it from the *realized*
    outcome of the curve it last broadcast:

    * if recent steps shed best-effort traffic (``ADMISSION_SHED``) **while beams sat idle**,
      the low-score buckets were too strict — raise them (the exact "rejecting with capacity to
      spare" failure the offered-load curve cannot see);
    * if recent steps showed congestion collapse, lower them.

    Updates are graded by score (low-score buckets move most; the top of the curve stays pinned
    admit-all, so high-value traffic is never shed) and the feedback signals are EWMA-smoothed to
    damp per-window noise. Fairness weights, airtime budgets, and fleet hints are taken verbatim
    from the heuristic planner — only the admission curve is learned.
    """

    def __init__(
        self,
        valid_for_steps: int = 1,
        *,
        buckets: int = 8,
        learning_rate: float = 0.3,
        signal_smoothing: float = 0.5,
        floor_min: float = 0.05,
        collapse_penalty: float = 1.0,
        util_clamp: float = 10.0,
        fairness_min: float = 0.5,
        fairness_max: float = 1.5,
    ) -> None:
        if buckets < 1:
            raise ValueError("buckets must be >= 1")
        self._valid_for_steps = valid_for_steps
        self._lr = learning_rate
        self._beta = signal_smoothing
        self._floor_min = floor_min
        self._collapse_penalty = collapse_penalty
        # Bucket score midpoints in (0, 1) and their learning gain (lower score -> larger gain).
        self._mids = tuple((i + 0.5) / buckets for i in range(buckets))
        self._grades = tuple(1.0 - m for m in self._mids)
        # Persistent learned state: start permissive (admit all), tighten only on evidence.
        self._p = [1.0] * buckets
        # EWMA-smoothed feedback signals (None until the first window that carries counters).
        self._u_ewma: float | None = None
        self._collapse_ewma: float | None = None
        self._shed_ewma: float | None = None
        self._base = HeuristicOptimizer(
            valid_for_steps,
            util_clamp=util_clamp,
            fairness_min=fairness_min,
            fairness_max=fairness_max,
        )

    def plan(self, window: PlanningWindow) -> ResourcePlan:
        self._learn(window)
        # Reuse the heuristic's fairness/budgets/hints; swap in the learned admission curve.
        return replace(self._base.plan(window), admission_curve=self._curve())

    def _learn(self, window: PlanningWindow) -> None:
        # Only the steps the previously broadcast curve actually governed are feedback.
        recent = window.recent_counters[-self._valid_for_steps :]
        if not recent:
            return  # no realized outcome yet; keep the current (initially admit-all) curve

        u_real = sum(c.utilization for c in recent) / len(recent)
        collapse = sum(c.collapse_risk for c in recent) / len(recent)
        handled = 0
        shed = 0
        for c in recent:
            handled += c.served + c.deferred + c.dropped + c.rejected
            shed += c.rejected_by_reason.get(RejectReason.ADMISSION_SHED, 0)
        shed_frac = shed / handled if handled > 0 else 0.0

        # EWMA-smooth the noisy per-window signals before acting on them.
        self._u_ewma = self._ewma(self._u_ewma, u_real)
        self._collapse_ewma = self._ewma(self._collapse_ewma, collapse)
        self._shed_ewma = self._ewma(self._shed_ewma, shed_frac)

        idle = max(0.0, 1.0 - self._u_ewma)
        # Net signed signal: loosen when we shed *while* capacity sat idle; tighten on collapse.
        signal = idle * self._shed_ewma - self._collapse_penalty * self._collapse_ewma
        for i, grade in enumerate(self._grades):
            self._p[i] = _clamp(self._p[i] + self._lr * grade * signal, self._floor_min, 1.0)
        # Keep the curve monotone non-decreasing in score (higher score never less likely).
        for i in range(1, len(self._p)):
            self._p[i] = max(self._p[i], self._p[i - 1])

    def _ewma(self, prev: float | None, value: float) -> float:
        return value if prev is None else (1.0 - self._beta) * prev + self._beta * value

    def _curve(self) -> AdmissionCurve:
        # Endpoints anchor the curve: the score-0 floor and an always-admit (1.0, 1.0) ceiling.
        points = [(0.0, self._p[0]), *zip(self._mids, self._p, strict=True), (1.0, 1.0)]
        return AdmissionCurve(breakpoints=tuple(points))


# --------------------------------------------------------------------------- factory


def build_optimizer(config: SimulationConfig) -> Optimizer:
    """Construct the Tier-3 optimizer selected by ``config.optimizer.backend``."""
    opt = config.optimizer
    tuning = {
        "util_clamp": opt.utilization_clamp,
        "fairness_min": opt.fairness_weight_min,
        "fairness_max": opt.fairness_weight_max,
    }
    if opt.backend is OptimizerBackend.SOLVER:
        return SolverOptimizer(
            valid_for_steps=opt.cadence_steps,
            time_limit_s=opt.solver_time_limit_s,
            served_weight=opt.served_weight,
            fairness_bonus_weight=opt.fairness_bonus_weight,
            assignment_weight=opt.assignment_weight,
            switching_penalty_weight=opt.switching_penalty_weight,
            congestion_penalty_weight=opt.congestion_penalty_weight,
            soft_utilization=opt.soft_utilization,
            **tuning,
        )
    if opt.backend is OptimizerBackend.ADAPTIVE:
        return AdaptiveOptimizer(
            valid_for_steps=opt.cadence_steps,
            buckets=opt.adaptive_buckets,
            learning_rate=opt.adaptive_learning_rate,
            signal_smoothing=opt.adaptive_signal_smoothing,
            floor_min=opt.adaptive_floor_min,
            collapse_penalty=opt.adaptive_collapse_penalty,
            **tuning,
        )
    return HeuristicOptimizer(valid_for_steps=opt.cadence_steps, **tuning)
