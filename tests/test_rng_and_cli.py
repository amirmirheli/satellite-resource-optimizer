"""Tests for the seedable RNG (determinism) and the CLI entry point."""

from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import ValidationError

from satsim.cli import SCENARIOS, main
from satsim.rng import Rng
from satsim.scenarios import build_scenario


def test_rng_is_deterministic_for_same_seed() -> None:
    a_rng = Rng(42)
    b_rng = Rng(42)
    a = [a_rng.random() for _ in range(5)]
    b = [b_rng.random() for _ in range(5)]
    assert a == b


def test_rng_differs_across_seeds() -> None:
    assert Rng(1).random() != Rng(2).random()


def test_rng_derive_is_stable_and_independent() -> None:
    base = Rng(7)
    assert base.derive("demand").seed == Rng(7).derive("demand").seed
    assert base.derive("demand").seed != base.derive("constellation").seed


def test_rng_derive_is_stable_across_processes() -> None:
    script = "from satsim.rng import Rng; print(Rng(7).derive('demand').seed)"
    a = subprocess.check_output([sys.executable, "-c", script], text=True).strip()
    b = subprocess.check_output([sys.executable, "-c", script], text=True).strip()
    assert a == b


def test_poisson_is_nonnegative_and_zero_rate() -> None:
    rng = Rng(0)
    assert rng.poisson(0.0) == 0
    assert all(rng.poisson(5.0) >= 0 for _ in range(20))


def test_cli_no_scenario_lists_scenarios(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main([]) == 0
    out = capsys.readouterr().out
    for name in SCENARIOS:
        assert name in out


def test_cli_selects_scenario() -> None:
    # Keep it short; a small positive step count is valid and fast.
    assert main(["emergency_surge", "--seed", "3", "--steps", "5"]) == 0


def test_cli_rejects_non_positive_steps() -> None:
    with pytest.raises(SystemExit):
        main(["emergency_surge", "--steps", "0"])


def test_scenario_override_revalidates_config() -> None:
    with pytest.raises(ValidationError):
        build_scenario("mixed_roadmap", steps=0)
