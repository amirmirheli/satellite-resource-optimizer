"""Port: constellation client (heterogeneous fleet backend).

Each fleet (legacy LEO, next-gen) is exposed through its own ``ConstellationClient``.
The control loop merges per-fleet snapshots, so it never assumes a homogeneous network.
Lookups may fail; failures are caught and counted upstream, tripping a circuit breaker
that fails over to the other fleet where legal.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from satsim.domain.enums import FleetId
from satsim.domain.models import ConstellationSnapshot


class ConstellationError(RuntimeError):
    """Raised when a fleet's resource lookup fails (drives the circuit breaker)."""


@runtime_checkable
class ConstellationClient(Protocol):
    """Exposes currently-visible satellites/beams and per-region coverage for one fleet."""

    def fleet_id(self) -> FleetId:
        """Identify which fleet this client represents."""
        ...

    def visible_resources(self, step: int) -> ConstellationSnapshot:
        """Return the satellites/beams visible at ``step`` with per-beam capacity.

        Raises:
            ConstellationError: if the fleet cannot be reached / the lookup fails.
        """
        ...
