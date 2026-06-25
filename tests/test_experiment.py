"""Tests for the UI-free experiment API (what the Streamlit app is a shell over)."""

from __future__ import annotations

from satsim.config import SimulationConfig
from satsim.domain.enums import RejectReason
from satsim.domain.telemetry import StepCounters
from satsim.runtime.experiment import (
    ExperimentResult,
    capacity_slack_summary,
    disposition_series,
    health_series,
    latency_percentiles,
    rejection_reason_totals,
    run_experiment,
    served_by_class_totals,
    utilization_vs_demand_series,
)


def test_run_experiment_returns_summary_and_steps() -> None:
    result = run_experiment(SimulationConfig(seed=1, duration_steps=12))
    assert isinstance(result, ExperimentResult)
    assert result.summary.steps == 12
    assert len(result.steps) == 12


def test_series_lengths_match_steps() -> None:
    result = run_experiment(SimulationConfig(seed=2, duration_steps=10))
    for series in (disposition_series(result.steps), health_series(result.steps)):
        assert series
        assert all(len(values) == 10 for values in series.values())


def test_disposition_series_keys() -> None:
    result = run_experiment(SimulationConfig(seed=3, duration_steps=5))
    assert set(disposition_series(result.steps)) == {"served", "deferred", "dropped", "rejected"}


def test_totals_are_chartable_dicts() -> None:
    result = run_experiment(SimulationConfig(seed=4, duration_steps=20))
    by_class = served_by_class_totals(result.summary)
    reasons = rejection_reason_totals(result.steps)
    assert all(isinstance(k, str) for k in by_class)
    assert all(isinstance(k, str) for k in reasons)
    # Served-by-class totals reconcile with the summary's overall served count.
    assert sum(by_class.values()) == result.summary.served


def test_experiment_is_deterministic() -> None:
    a = run_experiment(SimulationConfig(seed=9, duration_steps=15)).summary
    b = run_experiment(SimulationConfig(seed=9, duration_steps=15)).summary
    assert (a.served, a.dropped, a.rejected) == (b.served, b.dropped, b.rejected)


def test_latency_percentiles_are_ordered() -> None:
    # Waits 0..9 across two steps: p50<=p95<=p99<=max, and max is the largest wait.
    steps = [
        StepCounters(step=0, served_wait_steps=(0, 1, 2, 3, 4)),
        StepCounters(step=1, served_wait_steps=(5, 6, 7, 8, 9)),
    ]
    lat = latency_percentiles(steps)
    assert lat["count"] == 10.0
    assert lat["p50"] <= lat["p95"] <= lat["p99"] <= lat["max"] == 9.0


def test_latency_percentiles_empty_is_zero() -> None:
    lat = latency_percentiles([StepCounters(step=0)])
    assert lat == {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "count": 0.0}


def test_demand_pressure_and_no_waste_when_idle_by_design() -> None:
    # Little offered load, no rejections -> demand pressure < 1, nothing wasted.
    steps = [
        StepCounters(step=i, utilization=0.1, offered_load_units=10.0,
                     available_capacity_units=100.0)
        for i in range(5)
    ]
    slack = capacity_slack_summary(steps)
    assert slack["mean_demand_pressure"] == 0.1   # offered 10 / capacity 100
    assert slack["by_design_reject_frac"] == 0.0  # no rejections at all
    assert slack["scarcity_while_idle"] == 0.0


def test_value_shed_counts_as_by_design_not_waste() -> None:
    # Admission shedding is deliberate value-policy: by-design, never scarcity-while-idle.
    steps = [
        StepCounters(
            step=0, utilization=0.2, offered_load_units=200.0, available_capacity_units=100.0,
            rejected_by_reason={RejectReason.ADMISSION_SHED: 7},
        )
    ]
    slack = capacity_slack_summary(steps)
    assert slack["by_design_reject_frac"] == 1.0
    assert slack["scarcity_while_idle"] == 0.0


def test_scarcity_drop_while_idle_flags_as_waste() -> None:
    # Dropped for no-capacity while beams idle (util < 0.9) -> the wasteful upper-bound signal.
    steps = [
        StepCounters(
            step=0, utilization=0.2, offered_load_units=20.0, available_capacity_units=100.0,
            rejected_by_reason={RejectReason.NO_CAPACITY: 3, RejectReason.NO_LEGAL_OPTION: 2},
        )
    ]
    slack = capacity_slack_summary(steps)
    assert slack["scarcity_while_idle"] == 3.0          # the NO_CAPACITY drops
    assert slack["by_design_reject_frac"] == 2 / 5      # NO_LEGAL_OPTION is structural


def test_utilization_vs_demand_series_aligns() -> None:
    result = run_experiment(SimulationConfig(seed=5, duration_steps=10))
    series = utilization_vs_demand_series(result.steps)
    assert set(series) == {"utilization", "demand_pressure"}
    assert all(len(v) == 10 for v in series.values())
