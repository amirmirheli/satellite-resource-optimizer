"""Default :class:`~satsim.ports.request_source.RequestSource`: synthetic, bus-backed.

Wires the producer (:class:`~satsim.demand.DemandGenerator`) to the consumer side: each
``poll`` ensures this step's demand has been published exactly once, then drains up to
``max_batch`` from the :class:`~satsim.bus.InMemoryBus`. Whatever isn't drained remains
as backlog (consumer lag), exactly as a real consumer would see.
"""

from __future__ import annotations

from satsim.bus import InMemoryBus
from satsim.config import SimulationConfig
from satsim.demand import DemandGenerator
from satsim.domain.models import ServiceRequest
from satsim.rng import Rng


class SyntheticRequestSource:
    """Consume-side fake backed by a DemandGenerator publishing onto an InMemoryBus."""

    def __init__(self, config: SimulationConfig, rng: Rng, bus: InMemoryBus | None = None) -> None:
        self._bus = bus if bus is not None else InMemoryBus()
        self._generator = DemandGenerator(config, rng, self._bus)
        self._generated_through = -1  # highest step already produced

    @property
    def backlog(self) -> int:
        """Undrained requests currently buffered on the bus (consumer lag)."""
        return len(self._bus)

    def poll(self, step: int, max_batch: int) -> list[ServiceRequest]:
        """Produce this step's demand (once) and drain up to ``max_batch`` requests."""
        while self._generated_through < step:
            self._generated_through += 1
            self._generator.generate_step(self._generated_through)
        return self._bus.drain(step, max_batch)
