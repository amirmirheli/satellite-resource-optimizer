"""Tests for the UI-free experiment API (what the Streamlit app is a shell over)."""

from __future__ import annotations

from satsim.config import SimulationConfig
from satsim.experiment import (
    ExperimentResult,
    disposition_series,
    health_series,
    rejection_reason_totals,
    run_experiment,
    served_by_class_totals,
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
