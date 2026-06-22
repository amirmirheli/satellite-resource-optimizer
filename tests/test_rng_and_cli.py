"""Tests for the seedable RNG (determinism) and the CLI entry point."""

from __future__ import annotations

from satsim.cli import SCENARIOS, main
from satsim.rng import Rng


def test_rng_is_deterministic_for_same_seed() -> None:
    a = [Rng(42).random() for _ in range(5)]
    b = [Rng(42).random() for _ in range(5)]
    assert a == b


def test_rng_differs_across_seeds() -> None:
    assert Rng(1).random() != Rng(2).random()


def test_rng_derive_is_stable_and_independent() -> None:
    base = Rng(7)
    assert base.derive("demand").seed == Rng(7).derive("demand").seed
    assert base.derive("demand").seed != base.derive("constellation").seed


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
    # Keep it short; build_scenario(model_copy) skips re-validation so a small step count is fine.
    assert main(["emergency_surge", "--seed", "3", "--steps", "5"]) == 0
