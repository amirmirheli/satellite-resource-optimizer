"""Tests for env-driven RunSettings and CLI precedence."""

from __future__ import annotations

import pytest

from satsim.cli import _apply_run_overrides, main
from satsim.config import SimulationConfig
from satsim.domain.enums import OptimizerBackend, SchedulerKind
from satsim.scenarios import build_scenario
from satsim.settings import RunSettings


def test_defaults_have_no_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure no stray SATSIM_ vars leak in from the environment.
    for key in ("SEED", "STEPS", "SCHEDULER", "OPTIMIZER_BACKEND", "VERBOSE"):
        monkeypatch.delenv(f"SATSIM_{key}", raising=False)
    settings = RunSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.overrides() == set()


def test_env_values_are_read_and_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SATSIM_SEED", "99")
    monkeypatch.setenv("SATSIM_SCHEDULER", "heuristic")
    settings = RunSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.seed == 99
    assert settings.scheduler is SchedulerKind.HEURISTIC
    assert {"seed", "scheduler"} <= settings.overrides()


def test_apply_run_overrides_only_for_env_set() -> None:
    config = build_scenario("mixed_roadmap")  # defaults to PRIORITY_FAIR / heuristic backend
    settings = RunSettings(
        scheduler=SchedulerKind.HEURISTIC,
        optimizer_backend=OptimizerBackend.SOLVER,
    )
    # Pretend only the scheduler was provided via env.
    updated = _apply_run_overrides(config, settings, {"scheduler"})
    assert updated.scheduler is SchedulerKind.HEURISTIC
    assert updated.optimizer.backend is OptimizerBackend.HEURISTIC  # backend NOT overridden


def test_apply_run_overrides_optimizer_backend() -> None:
    config = build_scenario("mixed_roadmap")
    settings = RunSettings(optimizer_backend=OptimizerBackend.SOLVER, solver_time_limit_s=2.0)
    updated = _apply_run_overrides(
        config, settings, {"optimizer_backend", "solver_time_limit_s"}
    )
    assert updated.optimizer.backend is OptimizerBackend.SOLVER
    assert updated.optimizer.solver_time_limit_s == 2.0


def test_no_overrides_returns_same_config() -> None:
    config = build_scenario("mixed_roadmap")
    settings = RunSettings(_env_file=None)  # type: ignore[call-arg]
    assert _apply_run_overrides(config, settings, set()) is config


def test_cli_env_seed_takes_effect(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SATSIM_SEED", "77")
    assert main(["emergency_surge", "--steps", "3"]) == 0
    assert "seed=77" in capsys.readouterr().out


def test_cli_flag_overrides_env_seed(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SATSIM_SEED", "77")
    assert main(["emergency_surge", "--seed", "5", "--steps", "3"]) == 0
    assert "seed=5" in capsys.readouterr().out


def test_config_exposes_new_tunable_submodels() -> None:
    config = SimulationConfig()
    assert config.scoring.priority_weight == 0.6
    assert config.scheduler_params.degrade_min_fraction == 0.25
