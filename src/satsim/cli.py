"""Command-line entry point: run a named scenario and print its telemetry summary."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from pydantic import ValidationError

from satsim import __version__
from satsim.adapters.telemetry import ConsoleTelemetrySink, InMemoryTelemetrySink
from satsim.config import OptimizerConfig, SimulationConfig
from satsim.loop import RunSummary, build_simulation
from satsim.scenarios import SCENARIOS, build_scenario, scenario_names
from satsim.settings import RunSettings


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the ``satsim`` command."""
    parser = argparse.ArgumentParser(prog="satsim", description=__doc__)
    parser.add_argument("--version", action="version", version=f"satsim {__version__}")
    parser.add_argument(
        "scenario",
        nargs="?",
        choices=scenario_names(),
        help="Named scenario to run.",
    )
    parser.add_argument(
        "--seed", type=_non_negative_int, default=None, help="Override the scenario's RNG seed."
    )
    parser.add_argument(
        "--steps", type=_positive_int, default=None, help="Override simulation duration."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print per-step telemetry while running."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.scenario is None:
        _list_scenarios()
        return 0

    # Precedence: explicit CLI flag > env / .env > scenario default.
    try:
        settings = RunSettings()
        env_set = settings.overrides()
        env_seed = settings.seed if "seed" in env_set else None
        env_steps = settings.steps if "steps" in env_set else None
        seed = args.seed if args.seed is not None else env_seed
        steps = args.steps if args.steps is not None else env_steps

        config = build_scenario(args.scenario, seed=seed, steps=steps)
        config = _apply_run_overrides(config, settings, env_set)
    except ValidationError as exc:
        parser.error(str(exc))
    verbose = args.verbose or ("verbose" in env_set and settings.verbose)

    print(
        f"running '{args.scenario}' "
        f"(seed={config.seed}, steps={config.duration_steps}, scheduler={config.scheduler.value})…"
    )

    if verbose:
        # Per-step lines stream to stdout; no aggregate stats kept.
        loop = build_simulation(config, ConsoleTelemetrySink())
        summary = loop.run()
        _print_summary(args.scenario, summary)
    else:
        sink = InMemoryTelemetrySink()
        loop = build_simulation(config, sink)
        summary = loop.run()
        _print_summary(args.scenario, summary)
        _print_aggregates(sink)
    return 0


def _apply_run_overrides(
    config: SimulationConfig, settings: RunSettings, env_set: set[str]
) -> SimulationConfig:
    """Apply env-provided scheduler / optimizer overrides onto a scenario config."""
    updates: dict[str, object] = {}
    if "scheduler" in env_set:
        updates["scheduler"] = settings.scheduler

    opt_updates: dict[str, object] = {}
    if "optimizer_backend" in env_set:
        opt_updates["backend"] = settings.optimizer_backend
    if "solver_time_limit_s" in env_set:
        opt_updates["solver_time_limit_s"] = settings.solver_time_limit_s
    if opt_updates:
        opt_data = config.optimizer.model_dump()
        opt_data.update(opt_updates)
        updates["optimizer"] = OptimizerConfig.model_validate(opt_data)

    if not updates:
        return config
    data = config.model_dump()
    data.update(updates)
    return SimulationConfig.model_validate(data)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def _list_scenarios() -> None:
    print("satsim — satellite resource-optimization simulator\n")
    print("available scenarios:")
    for name in scenario_names():
        print(f"  {name:<24} {SCENARIOS[name].description}")
    print("\nRun one with:  satsim <scenario>  [--seed N] [--steps N] [--verbose]")


def _print_summary(scenario: str, summary: RunSummary) -> None:
    total = summary.served + summary.dropped + summary.rejected
    served_pct = (100.0 * summary.served / total) if total > 0 else 0.0
    print(f"\n=== {scenario} summary ===")
    print(f"steps        : {summary.steps}")
    print(f"served       : {summary.served} ({served_pct:.1f}% of resolved)")
    print(f"deferred     : {summary.deferred}")
    print(f"dropped      : {summary.dropped}")
    print(f"rejected     : {summary.rejected}")
    print(f"retry backlog: {summary.retry_backlog}")
    print(f"fallbacks    : {summary.fallback_activations}")
    print(f"breaker trips: {summary.circuit_breaker_trips}")
    if summary.served_by_class:
        print("served by class:")
        for tc, n in sorted(summary.served_by_class.items(), key=lambda kv: -kv[0].value):
            print(f"  {tc.name:<16} {n}")


def _print_aggregates(sink: InMemoryTelemetrySink) -> None:
    steps = sink.steps
    if not steps:
        return
    avg_util = sum(s.utilization for s in steps) / len(steps)
    peak_collapse = max(s.collapse_risk for s in steps)
    degraded_allocs = sum(s.degraded for s in steps)
    degraded_mode_steps = sum(1 for s in steps if s.degrade_mode.value != "normal")

    reasons: dict[str, int] = {}
    for s in steps:
        for reason, n in s.rejected_by_reason.items():
            reasons[reason.value] = reasons.get(reason.value, 0) + n

    print(f"avg utilization   : {avg_util:.2f}")
    print(f"peak collapse     : {peak_collapse:.2f}")
    print(f"degraded served   : {degraded_allocs}")
    print(f"degraded-mode steps: {degraded_mode_steps}")
    if reasons:
        print("rejections by reason:")
        for label, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {label:<24} {count}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
