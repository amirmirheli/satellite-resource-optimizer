"""Interactive Streamlit front-end for the satellite resource-optimization simulator.

Run with:  uv run --group ui streamlit run streamlit_app.py

A thin UI over :mod:`satsim.experiment`: pick a scenario, tweak parameters in the sidebar, run,
and chart the results. All the real logic lives in the (UI-free, tested) library.
"""

from __future__ import annotations

import logging
import time

import streamlit as st

from satsim.config import SimulationConfig, SurgeEvent
from satsim.domain.enums import OptimizerBackend, SchedulerKind, TrafficClass
from satsim.domain.telemetry import StepCounters
from satsim.experiment import (
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
from satsim.scenarios import SCENARIOS, build_scenario, scenario_names

# Logs go to the terminal hosting `streamlit run` (charts go to the browser). One line per run
# so the re-execution model is visible from the shell.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("satsim.streamlit")
logger.setLevel(logging.INFO)

_CUSTOM = "(custom defaults)"


def _run_simulation(config: SimulationConfig) -> ExperimentResult:
    """Run one simulation, logging its parameters and outcome to the terminal."""
    logger.info(
        "run start: seed=%d steps=%d scheduler=%s optimizer=%s baseline=%.1f surges=%d",
        config.seed,
        config.duration_steps,
        config.scheduler.value,
        config.optimizer.backend.value,
        config.arrival.baseline_rate,
        len(config.surges),
    )
    start = time.perf_counter()
    result = run_experiment(config)
    elapsed = time.perf_counter() - start
    s = result.summary
    logger.info(
        "run done in %.3fs: served=%d deferred=%d dropped=%d rejected=%d backlog=%d",
        elapsed, s.served, s.deferred, s.dropped, s.rejected, s.retry_backlog,
    )
    return result


def _build_config() -> SimulationConfig:
    """Read sidebar widgets and assemble a SimulationConfig (scenario base + overrides)."""
    st.sidebar.header("Scenario")
    choice = st.sidebar.selectbox("Base scenario", [_CUSTOM, *scenario_names()])
    if choice != _CUSTOM:
        st.sidebar.caption(SCENARIOS[choice].description)
    base = SimulationConfig() if choice == _CUSTOM else build_scenario(choice)

    st.sidebar.header("Run")
    seed = st.sidebar.number_input("Seed", min_value=0, value=int(base.seed), step=1)
    steps = st.sidebar.slider("Duration (steps)", 10, 300, int(base.duration_steps), 10)
    scheduler = st.sidebar.selectbox(
        "Scheduler", list(SchedulerKind), index=list(SchedulerKind).index(base.scheduler),
        format_func=lambda k: k.value,
    )
    backend = st.sidebar.selectbox(
        "Tier-3 optimizer", list(OptimizerBackend),
        index=list(OptimizerBackend).index(base.optimizer.backend),
        format_func=lambda b: b.value,
    )

    st.sidebar.header("Load")
    baseline = st.sidebar.slider(
        "Baseline arrivals / step", 0.0, 400.0, float(base.arrival.baseline_rate), 5.0
    )
    link_max = st.sidebar.slider(
        "Max link quality", 0.1, 1.0, float(base.arrival.link_quality_max), 0.05
    )

    st.sidebar.header("Capacity & resilience")
    reserved = st.sidebar.slider(
        "Emergency reserved fraction", 0.0, 1.0, float(base.emergency.reserved_fraction), 0.05
    )
    queue_cap = st.sidebar.slider(
        "Retry queue capacity", 16, 8192, int(base.overload.queue_capacity), 16
    )

    st.sidebar.header("Emergency surge")
    surge_on = st.sidebar.checkbox("Inject SOS surge", value=bool(base.surges))
    surge_step = st.sidebar.slider("Surge at step", 0, steps - 1, min(5, steps - 1))
    surge_count = st.sidebar.slider("Surge size", 100, 5000, 2000, 100)

    link_min = min(base.arrival.link_quality_min, link_max)
    arrival = base.arrival.model_copy(
        update={
            "baseline_rate": baseline,
            "link_quality_max": link_max,
            "link_quality_min": link_min,
        }
    )
    emergency_data = base.emergency.model_dump()
    emergency_data["reserved_fraction"] = reserved
    overload_data = base.overload.model_dump()
    overload_data["queue_capacity"] = int(queue_cap)
    optimizer_data = base.optimizer.model_dump()
    optimizer_data["backend"] = backend

    updates: dict[str, object] = {
        "seed": int(seed),
        "duration_steps": int(steps),
        "scheduler": scheduler,
        "arrival": arrival,
        "emergency": type(base.emergency).model_validate(emergency_data),
        "overload": type(base.overload).model_validate(overload_data),
        "optimizer": type(base.optimizer).model_validate(optimizer_data),
    }
    if surge_on:
        updates["surges"] = (
            SurgeEvent(at_step=int(surge_step), count=int(surge_count),
                       traffic_class=TrafficClass.EMERGENCY_SOS),
        )
    else:
        updates["surges"] = ()
    data = base.model_dump()
    data.update(updates)
    return SimulationConfig.model_validate(data)


def _render_utilization_health(steps: list[StepCounters]) -> None:
    """Show that low utilization reflects policy/structure, not wasted capacity."""
    st.subheader("Is the idle capacity wasted?")
    slack = capacity_slack_summary(steps)
    pressure = slack["mean_demand_pressure"]
    by_design = slack["by_design_reject_frac"]
    scarcity_idle = int(slack["scarcity_while_idle"])

    a, b, c = st.columns(3)
    a.metric(
        "Demand pressure", f"{pressure:.1f}×",
        "offered ÷ capacity — ≫1 means beams weren't idle for lack of work",
    )
    b.metric(
        "By-design / structural", f"{by_design * 100:.0f}%",
        "of rejections were value-shed or had no legal fleet (not 'out of room')",
    )
    c.metric(
        "Scarcity drops while idle", f"{scarcity_idle}",
        "dropped for no-capacity/deadline while beams idle (upper-bound waste) — low is good",
        delta_color="inverse",
    )
    st.caption(
        f"Beams ran at modest utilization while offered **{pressure:.1f}×** their capacity, and "
        f"**{by_design * 100:.0f}%** of unserved load was shed by value-policy or had no legal "
        f"fleet — both by design. Only **{scarcity_idle}** requests dropped for scarcity while "
        "capacity sat idle (an upper bound; most of that is regional/spectrum mismatch that idle "
        "beams elsewhere can't legally serve). Low utilization here is a value/coverage outcome, "
        "not fumbled capacity. The overlay below shows utilization tracking demand pressure."
    )
    st.line_chart(utilization_vs_demand_series(steps))


def _render_latency(steps: list[StepCounters], time_step_seconds: float) -> None:
    """Serve-latency percentiles (the tail the average hides) in steps and seconds."""
    st.subheader("Serve latency (end-to-end, arrival → served)")
    lat = latency_percentiles(steps)

    def _fmt(steps_value: float) -> str:
        return f"{steps_value:.0f} steps · {steps_value * time_step_seconds:.1f}s"

    p1, p2, p3, p4 = st.columns(4)
    p1.metric("p50 (median)", _fmt(lat["p50"]))
    p2.metric("p95", _fmt(lat["p95"]))
    p3.metric("p99", _fmt(lat["p99"]))
    p4.metric("max", _fmt(lat["max"]))
    st.caption(
        f"Across {int(lat['count'])} served requests. p95/p99 expose the slow tail — a few "
        "requests that waited through several retries — that the average smooths over."
    )


def main() -> None:
    st.set_page_config(page_title="Satellite Resource Optimizer", layout="wide")
    st.title("🛰️ Satellite Resource Optimization Simulator")
    st.caption(
        "Three-tier control plane: deterministic loop · reactive emergency lane · "
        "periodic global optimizer. Adjust parameters in the sidebar, then click Run."
    )

    config = _build_config()

    # Gate the (re-)run behind an explicit button: tweak several parameters, then run once.
    st.sidebar.header("Run control")
    if st.sidebar.button("▶ Run simulation", type="primary", use_container_width=True):
        st.session_state["result"] = _run_simulation(config)
        st.session_state["config"] = config

    if "result" not in st.session_state:
        st.info("👈 Set parameters in the sidebar, then click **▶ Run simulation**.")
        return

    # Render the last run; warn if the sidebar has drifted from what was actually run.
    if config != st.session_state["config"]:
        st.warning("Parameters changed since the last run — click **▶ Run simulation** to refresh.")
    config = st.session_state["config"]
    result = st.session_state["result"]
    summary, steps = result.summary, result.steps

    resolved = summary.served + summary.dropped + summary.rejected
    served_pct = (100.0 * summary.served / resolved) if resolved else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Served", f"{summary.served}", f"{served_pct:.0f}% of resolved")
    c2.metric("Dropped", f"{summary.dropped}")
    c3.metric("Rejected (shed)", f"{summary.rejected}")
    c4.metric("Retry backlog", f"{summary.retry_backlog}")

    c5, c6, c7 = st.columns(3)
    avg_util = sum(s.utilization for s in steps) / len(steps) if steps else 0.0
    peak_collapse = max((s.collapse_risk for s in steps), default=0.0)
    c5.metric("Avg utilization", f"{avg_util:.2f}")
    c6.metric("Peak collapse risk", f"{peak_collapse:.2f}")
    c7.metric(
        "Fallbacks / breaker trips",
        f"{summary.fallback_activations} / {summary.circuit_breaker_trips}",
    )

    _render_utilization_health(steps)
    _render_latency(steps, config.time_step_seconds)

    left, right = st.columns(2)
    with left:
        st.subheader("Dispositions per step")
        st.line_chart(disposition_series(steps))
        st.subheader("Served by class")
        st.bar_chart(served_by_class_totals(summary))
    with right:
        st.subheader("System health per step")
        st.line_chart(health_series(steps))
        st.subheader("Rejections by reason")
        st.bar_chart(rejection_reason_totals(steps))

    with st.expander("Resolved configuration (JSON)"):
        st.json(config.model_dump(mode="json"))


if __name__ == "__main__":
    main()
