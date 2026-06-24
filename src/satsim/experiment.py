"""Programmatic experiment API — the seam a UI (or a sweep script) drives.

Deliberately free of any UI dependency: build a :class:`~satsim.config.SimulationConfig`, call
:func:`run_experiment`, and read back a summary plus per-step series ready for charting. This is
what the Streamlit app (``streamlit_app.py``) is a thin shell over, and it keeps the runnable
surface testable without importing Streamlit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from satsim.adapters.telemetry import InMemoryTelemetrySink
from satsim.config import SimulationConfig
from satsim.domain.enums import RejectReason
from satsim.domain.telemetry import StepCounters
from satsim.loop import RunSummary, build_simulation

# Utilization below which a step is judged to have had spare capacity (for the waste signal).
_SPARE_UTILIZATION = 0.9


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


def demand_pressure_series(steps: list[StepCounters]) -> list[float]:
    """Per-step offered best-effort load ÷ schedulable capacity (>1 = demand exceeded capacity)."""
    out: list[float] = []
    for s in steps:
        cap = s.available_capacity_units
        out.append(s.offered_load_units / cap if cap > 0.0 else 0.0)
    return out


def utilization_vs_demand_series(steps: list[StepCounters]) -> dict[str, list[float]]:
    """Per-step realized utilization overlaid on demand pressure (for a line chart).

    The headline evidence that idle capacity is *not* waste: when utilization is low because
    demand pressure is also low, nobody was turned away — the capacity simply wasn't asked for.
    """
    return {
        "utilization": [s.utilization for s in steps],
        "demand_pressure": demand_pressure_series(steps),
    }


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted, non-empty list."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


def latency_percentiles(steps: list[StepCounters]) -> dict[str, float]:
    """End-to-end serve latency percentiles (in steps) across every served request in the run."""
    waits = sorted(float(w) for s in steps for w in s.served_wait_steps)
    return {
        "p50": _percentile(waits, 50.0),
        "p95": _percentile(waits, 95.0),
        "p99": _percentile(waits, 99.0),
        "max": waits[-1] if waits else 0.0,
        "count": float(len(waits)),
    }


# Rejections that are deliberate (value-policy shedding) or structural (no legal fleet for the
# region) — i.e. *not* "we ran out of room": idle capacity elsewhere could not have served them.
_BY_DESIGN_REASONS = frozenset({RejectReason.ADMISSION_SHED, RejectReason.NO_LEGAL_OPTION})
# Rejections that mean "couldn't fit": if these happen while beams are idle, that idle is the
# genuinely wasteful kind (an upper bound — much of it is regional/spectrum mismatch, unavoidable).
_SCARCITY_REASONS = frozenset({RejectReason.NO_CAPACITY, RejectReason.DEADLINE_MISSED})


def capacity_slack_summary(steps: list[StepCounters]) -> dict[str, float]:
    """Decompose *why* utilization is low: offered-but-unservable vs. genuinely wasted capacity.

    * ``mean_demand_pressure`` — average offered ÷ schedulable capacity. **≫ 1 means beams were
      not idle for lack of work** — far more was offered than could be held — so low utilization
      is a policy/structure outcome, not under-demand.
    * ``by_design_reject_frac`` — share of all rejections that were value-policy shedding or had
      no legal fleet for their region (by design / structural). High ⇒ unserved load was unserved
      for legitimate reasons, not fumbled capacity.
    * ``scarcity_while_idle`` — requests dropped for *scarcity* (no capacity / missed deadline)
      during steps where utilization was below ``_SPARE_UTILIZATION``: an **upper bound** on truly
      wasted capacity (much is regional/spectrum mismatch that idle beams elsewhere can't serve).
    """
    pressures = demand_pressure_series(steps)
    mean_pressure = sum(pressures) / len(pressures) if pressures else 0.0
    by_design = 0
    total_rejected = 0
    scarcity_while_idle = 0
    for s in steps:
        spare = s.utilization < _SPARE_UTILIZATION
        for reason, n in s.rejected_by_reason.items():
            total_rejected += n
            if reason in _BY_DESIGN_REASONS:
                by_design += n
            elif spare and reason in _SCARCITY_REASONS:
                scarcity_while_idle += n
    return {
        "mean_demand_pressure": mean_pressure,
        "by_design_reject_frac": by_design / total_rejected if total_rejected else 0.0,
        "scarcity_while_idle": float(scarcity_while_idle),
    }
