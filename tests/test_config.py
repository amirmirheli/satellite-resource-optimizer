"""Validation tests for SimulationConfig."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from satsim.config import ClassProfile, SimulationConfig, SurgeEvent
from satsim.domain.enums import Region, SchedulerKind, TrafficClass


def test_default_config_is_valid(default_config: SimulationConfig) -> None:
    assert default_config.duration_steps > 0
    assert default_config.class_mix  # non-empty mix
    assert sum(p.weight for p in default_config.class_mix.values()) > 0.0
    assert default_config.scheduler is SchedulerKind.PRIORITY_FAIR


def test_config_is_frozen(default_config: SimulationConfig) -> None:
    with pytest.raises(ValidationError):
        default_config.seed = 99  # type: ignore[misc]


def test_config_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        SimulationConfig(does_not_exist=1)  # type: ignore[call-arg]


def test_class_profile_rejects_inverted_sizes() -> None:
    with pytest.raises(ValidationError):
        ClassProfile(
            weight=1.0,
            min_size_bytes=100,
            max_size_bytes=10,
            deadline_slack_steps=1,
            retry_budget=1,
        )


def test_empty_class_mix_rejected() -> None:
    with pytest.raises(ValidationError):
        SimulationConfig(class_mix={})


def test_zero_weight_mix_rejected() -> None:
    zero = {
        TrafficClass.MESSAGING: ClassProfile(
            weight=0.0, min_size_bytes=1, max_size_bytes=2, deadline_slack_steps=1, retry_budget=1
        )
    }
    with pytest.raises(ValidationError):
        SimulationConfig(class_mix=zero)


def test_surge_beyond_duration_rejected() -> None:
    with pytest.raises(ValidationError):
        SimulationConfig(
            duration_steps=10,
            surges=(SurgeEvent(at_step=50, count=100, region=Region.NA),),
        )


def test_duplicate_region_rule_rejected(default_config: SimulationConfig) -> None:
    rules = (*default_config.region_rules, default_config.region_rules[0])
    with pytest.raises(ValidationError):
        SimulationConfig(region_rules=rules)


def test_seed_override_roundtrips() -> None:
    assert SimulationConfig(seed=7).seed == 7
