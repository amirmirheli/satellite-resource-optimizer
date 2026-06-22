"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from satsim.config import SimulationConfig


@pytest.fixture
def default_config() -> SimulationConfig:
    """A valid simulation config built entirely from defaults."""
    return SimulationConfig()
