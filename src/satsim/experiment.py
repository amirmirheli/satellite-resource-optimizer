"""Programmatic experiment API — the seam a UI (or a sweep script) drives.

Deliberately free of any UI dependency: build a :class:`~satsim.config.SimulationConfig`, call
:func:`run_experiment`, and read back a summary plus per-step series ready for charting. This is
what the Streamlit app (``streamlit_app.py``) is a thin shell over, and it keeps the runnable
surface testable without importing Streamlit.
"""

from __future__ import annotations

from dataclasses import dataclass

from satsim.adapters.telemetry import InMemoryTelemetrySink
from satsim.config import SimulationConfig
from satsim.domain.telemetry import StepCounters
from satsim.loop import RunSummary, build_simulation


@dataclass(slots=True)
class ExperimentResult:
    """A completed run: the aggregate summary plus the per-step counters series."""

    summary: RunSummary
    steps: list[StepCounters]


def run_experiment(config: SimulationConfig) -> ExperimentResult:
    """Run a full simulation for ``config`` and return its summary + per-step counters."""
    sink = InMemoryTelemetrySink()
    summary = build_simulation(config, sink).run()
    return ExperimentResult(summary=summary, steps=list(sink.steps))


def disposition_series(steps: list[StepCounters]) -> dict[str, list[int]]:
    """Per-step served/deferred/dropped/rejected counts (for a line chart)."""
    return {
        "served": [s.served for s in steps],
        "deferred": [s.deferred for s in steps],
        "dropped": [s.dropped for s in steps],
        "rejected": [s.rejected for s in steps],
    }


def health_series(steps: list[StepCounters]) -> dict[str, list[float]]:
    """Per-step utilization / collapse-risk / fairness / queue depth (for a line chart)."""
    return {
        "utilization": [s.utilization for s in steps],
        "collapse_risk": [s.collapse_risk for s in steps],
        "fairness_index": [s.fairness_index for s in steps],
        "queue_depth": [float(s.queue_depth) for s in steps],
    }


def served_by_class_totals(summary: RunSummary) -> dict[str, int]:
    """Total served per traffic class, keyed by class name (for a bar chart)."""
    return {tc.name: n for tc, n in summary.served_by_class.items()}


def rejection_reason_totals(steps: list[StepCounters]) -> dict[str, int]:
    """Total rejections/drops per reason across the run, keyed by reason (for a bar chart)."""
    totals: dict[str, int] = {}
    for s in steps:
        for reason, n in s.rejected_by_reason.items():
            totals[reason.value] = totals.get(reason.value, 0) + n
    return totals
