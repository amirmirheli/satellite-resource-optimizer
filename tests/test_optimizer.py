"""Tests for the Tier-3 optimizers (heuristic default path + OR-Tools solver)."""

from __future__ import annotations

import pytest

from satsim.adapters.optimization import (
    AdaptiveOptimizer,
    HeuristicOptimizer,
    SolverOptimizer,
    build_optimizer,
)
from satsim.config import OptimizerConfig, SimulationConfig
from satsim.domain.enums import (
    Band,
    FleetId,
    OptimizerBackend,
    Region,
    RejectReason,
    TrafficClass,
)
from satsim.domain.models import (
    Beam,
    CongestionState,
    ConstellationSnapshot,
    Satellite,
)
from satsim.domain.planning import PlanningWindow
from satsim.domain.telemetry import StepCounters
from satsim.ports.optimizer import Optimizer


def _snapshot() -> ConstellationSnapshot:
    legacy = Satellite(
        "leg", FleetId.LEGACY_LEO,
        (Beam("l0", FleetId.LEGACY_LEO, "leg", Region.NA, Band.S, 5.0),),
    )
    nextgen = Satellite(
        "ng", FleetId.NEXT_GEN,
        (
            Beam("n0", FleetId.NEXT_GEN, "ng", Region.NA, Band.KU, 20.0),
            Beam("n1", FleetId.NEXT_GEN, "ng", Region.EU, Band.S, 12.0),
        ),
    )
    return ConstellationSnapshot(step=0, satellites=(legacy, nextgen))


def _window(
    *,
    util: float,
    drop: float = 0.0,
    demand: dict[TrafficClass, float] | None = None,
    served: dict[TrafficClass, int] | None = None,
    demand_by_region: dict[tuple[Region, TrafficClass], float] | None = None,
) -> PlanningWindow:
    cap = _snapshot().total_capacity()
    congestion = CongestionState(
        step=0, queue_depth=0, offered_load_units=util * cap,
        available_capacity_units=cap, drop_rate=drop,
    )
    counters = (StepCounters(step=0, served_by_class=served or {}),)
    by_region = demand_by_region or {}
    # If only per-region demand is given, derive the per-class aggregate so both stay consistent.
    if by_region and demand is None:
        demand = {}
        for (_r, tc), units in by_region.items():
            demand[tc] = demand.get(tc, 0.0) + units
    return PlanningWindow(
        step=10, snapshot=_snapshot(), recent_congestion=(congestion,),
        recent_demand_units=demand or {}, recent_demand_by_region=by_region,
        recent_counters=counters,
    )


# --------------------------------------------------------------------------- heuristic


def test_heuristic_satisfies_port() -> None:
    assert isinstance(HeuristicOptimizer(), Optimizer)


def test_low_congestion_admits_generously() -> None:
    plan = HeuristicOptimizer().plan(_window(util=0.5))
    assert plan.admission_curve.admit_probability(0.0) == 1.0  # no shedding when calm


def test_high_congestion_sheds_low_scores() -> None:
    plan = HeuristicOptimizer().plan(_window(util=2.0))
    curve = plan.admission_curve
    # Proportional shedding: score-0 admitted ~1/util (not collapsed to 0), score-1 still full,
    # and the curve is monotone (low scores shed harder than high scores).
    assert curve.admit_probability(0.0) == pytest.approx(0.5)  # ~1/util
    assert curve.admit_probability(1.0) == 1.0
    assert curve.admit_probability(0.0) < curve.admit_probability(1.0)


def test_drop_rate_increases_shedding() -> None:
    calm = HeuristicOptimizer().plan(_window(util=1.0, drop=0.0))
    dropping = HeuristicOptimizer().plan(_window(util=1.0, drop=0.6))
    calm_floor = calm.admission_curve.admit_probability(0.0)
    dropping_floor = dropping.admission_curve.admit_probability(0.0)
    assert dropping_floor < calm_floor


def test_starved_class_gets_boosted_weight() -> None:
    # MESSAGING and PHOTO both demanded equally, but only MESSAGING was served.
    window = _window(
        util=1.5,
        demand={TrafficClass.MESSAGING: 50.0, TrafficClass.PHOTO: 50.0},
        served={TrafficClass.MESSAGING: 40, TrafficClass.PHOTO: 0},
    )
    weights = HeuristicOptimizer().plan(window).fairness_weights
    assert weights[TrafficClass.PHOTO] > weights[TrafficClass.MESSAGING]


def test_fleet_hint_prefers_higher_capacity_fleet() -> None:
    # In NA, NEXT_GEN (20) outweighs LEGACY_LEO (5).
    hints = HeuristicOptimizer().plan(_window(util=1.0)).fleet_hints
    assert hints[Region.NA] is FleetId.NEXT_GEN


def test_budgets_are_demand_proportional() -> None:
    window = _window(
        util=1.0, demand={TrafficClass.MESSAGING: 30.0, TrafficClass.PHOTO: 10.0}
    )
    budgets = HeuristicOptimizer().plan(window).airtime_budgets
    # MESSAGING has 3x the demand share -> 3x the budget.
    assert budgets[TrafficClass.MESSAGING] == pytest.approx(3 * budgets[TrafficClass.PHOTO])


def test_empty_window_is_neutral() -> None:
    plan = HeuristicOptimizer(valid_for_steps=7).plan(
        PlanningWindow(step=3, snapshot=ConstellationSnapshot(step=3))
    )
    assert plan.valid_for_steps == 7
    assert plan.admission_curve.admit_probability(0.0) == 1.0
    assert plan.fairness_weights == {}


def test_factory_selects_backend() -> None:
    heuristic_cfg = SimulationConfig()
    assert isinstance(build_optimizer(heuristic_cfg), HeuristicOptimizer)
    solver_cfg = SimulationConfig(optimizer=OptimizerConfig(backend=OptimizerBackend.SOLVER))
    assert isinstance(build_optimizer(solver_cfg), SolverOptimizer)
    adaptive_cfg = SimulationConfig(optimizer=OptimizerConfig(backend=OptimizerBackend.ADAPTIVE))
    assert isinstance(build_optimizer(adaptive_cfg), AdaptiveOptimizer)


# --------------------------------------------------------------------------- adaptive


def _counters(
    *, utilization: float, collapse_risk: float = 0.0, served: int = 0, shed: int = 0
) -> StepCounters:
    return StepCounters(
        step=0,
        served=served,
        rejected=shed,
        utilization=utilization,
        collapse_risk=collapse_risk,
        rejected_by_reason={RejectReason.ADMISSION_SHED: shed} if shed else {},
    )


def _adaptive_window(counters: tuple[StepCounters, ...]) -> PlanningWindow:
    return PlanningWindow(step=10, snapshot=_snapshot(), recent_counters=counters)


def test_adaptive_satisfies_port() -> None:
    assert isinstance(AdaptiveOptimizer(), Optimizer)


def test_adaptive_bootstraps_admit_all() -> None:
    # No realized feedback yet -> the learned curve admits everything.
    plan = AdaptiveOptimizer(valid_for_steps=5).plan(
        PlanningWindow(step=0, snapshot=_snapshot())
    )
    assert plan.valid_for_steps == 5
    assert plan.admission_curve.admit_probability(0.0) == 1.0


def test_adaptive_tightens_low_scores_under_collapse() -> None:
    opt = AdaptiveOptimizer(valid_for_steps=1, learning_rate=1.0, signal_smoothing=1.0)
    # Beams full and offered load way over capacity -> shed low scores, never high.
    curve = opt.plan(
        _adaptive_window((_counters(utilization=1.0, collapse_risk=1.0, served=10, shed=5),))
    ).admission_curve
    assert curve.admit_probability(0.0) < curve.admit_probability(1.0)
    assert curve.admit_probability(1.0) == 1.0


def test_adaptive_recovers_when_shedding_with_idle_capacity() -> None:
    opt = AdaptiveOptimizer(valid_for_steps=1, learning_rate=0.5, signal_smoothing=1.0)
    # First, congestion collapse drives the low-score floor down.
    tightened = opt.plan(
        _adaptive_window((_counters(utilization=1.0, collapse_risk=1.0, served=10, shed=5),))
    ).admission_curve.admit_probability(0.0)
    # Then we shed heavily while beams sit idle -> the floor must climb back up.
    recovered = opt.plan(
        _adaptive_window((_counters(utilization=0.2, collapse_risk=0.0, served=2, shed=8),))
    ).admission_curve.admit_probability(0.0)
    assert recovered > tightened


def test_adaptive_curve_is_monotone_in_score() -> None:
    opt = AdaptiveOptimizer(valid_for_steps=1, learning_rate=1.0, signal_smoothing=1.0)
    curve = opt.plan(
        _adaptive_window((_counters(utilization=1.0, collapse_risk=0.8, served=10, shed=6),))
    ).admission_curve
    probs = [curve.admit_probability(s / 10.0) for s in range(11)]
    assert probs == sorted(probs)  # non-decreasing: higher score never less likely


def test_adaptive_idle_without_shedding_does_not_loosen() -> None:
    # Idle capacity but nothing was shed (e.g. a demand lull) -> no false "too strict" signal,
    # so an already-tightened floor is not spuriously raised.
    opt = AdaptiveOptimizer(valid_for_steps=1, learning_rate=0.5, signal_smoothing=1.0)
    tightened = opt.plan(
        _adaptive_window((_counters(utilization=1.0, collapse_risk=1.0, served=10, shed=5),))
    ).admission_curve.admit_probability(0.0)
    unchanged = opt.plan(
        _adaptive_window((_counters(utilization=0.1, collapse_risk=0.0, served=2, shed=0),))
    ).admission_curve.admit_probability(0.0)
    assert unchanged == pytest.approx(tightened)


# --------------------------------------------------------------------------- solver


@pytest.mark.solver
def test_solver_satisfies_port() -> None:
    assert isinstance(SolverOptimizer(), Optimizer)


@pytest.mark.solver
def test_solver_produces_valid_plan() -> None:
    window = _window(
        util=1.2,
        demand_by_region={
            (Region.NA, TrafficClass.EMERGENCY_SOS): 5.0,
            (Region.NA, TrafficClass.PHOTO): 100.0,
            (Region.EU, TrafficClass.MESSAGING): 30.0,
        },
    )
    plan = SolverOptimizer(time_limit_s=1.0).plan(window)
    # Every region with capacity gets a fleet hint.
    assert set(plan.fleet_hints) == {Region.NA, Region.EU}
    # Per-(region, class) budgets exist and never exceed total capacity.
    assert plan.region_class_budgets
    assert sum(plan.region_class_budgets.values()) <= _snapshot().total_capacity() + 1e-6
    # Fairness weights stay within the documented band.
    assert all(0.5 <= w <= 1.5 for w in plan.fairness_weights.values())


@pytest.mark.solver
def test_solver_budgets_respect_assigned_capacity_coupling() -> None:
    # The coupling constraint: each region's total budget <= the capacity available there.
    window = _window(
        util=2.0,
        demand_by_region={
            (Region.NA, TrafficClass.PHOTO): 500.0,   # huge demand in NA
            (Region.EU, TrafficClass.PHOTO): 500.0,
        },
    )
    plan = SolverOptimizer(time_limit_s=1.0).plan(window)
    per_region: dict[Region, float] = {}
    for (region, _c), units in plan.region_class_budgets.items():
        per_region[region] = per_region.get(region, 0.0) + units
    # Budget is bounded by the *assigned* (single) fleet's capacity, not the sum of both fleets.
    # NA's biggest single fleet is nextgen (20) — staying under 25 proves only one fleet is used.
    max_single_fleet = {Region.NA: 20.0, Region.EU: 12.0}
    for region, used in per_region.items():
        assert used <= max_single_fleet[region] + 1e-6


@pytest.mark.solver
def test_solver_is_deterministic() -> None:
    window = _window(
        util=1.2, demand_by_region={(Region.EU, TrafficClass.MESSAGING): 40.0}
    )
    a = SolverOptimizer(time_limit_s=1.0).plan(window)
    b = SolverOptimizer(time_limit_s=1.0).plan(window)
    assert a.fleet_hints == b.fleet_hints
    assert a.region_class_budgets == b.region_class_budgets


@pytest.mark.solver
def test_solver_falls_back_on_trivial_window() -> None:
    # No capacity -> _solve returns None -> heuristic fallback still yields a valid plan.
    plan = SolverOptimizer().plan(PlanningWindow(step=0, snapshot=ConstellationSnapshot(step=0)))
    assert plan.admission_curve.admit_probability(0.0) == 1.0
    assert plan.fleet_hints == {}
