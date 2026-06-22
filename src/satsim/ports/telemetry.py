"""Port: telemetry sink (structured observability).

Every decision (admit/reject, which constellation/beam, degrade/defer, safe-fallback)
is observable. A ``ConsoleTelemetrySink`` prints structured output; an
``InMemoryTelemetrySink`` retains events/counters for assertions in tests.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from satsim.domain.telemetry import StepCounters, TelemetryEvent


@runtime_checkable
class TelemetrySink(Protocol):
    """Receives per-decision events and per-step aggregate counters."""

    def emit(self, event: TelemetryEvent) -> None:
        """Record a single structured decision event."""
        ...

    def record_step(self, counters: StepCounters) -> None:
        """Record the aggregate counters for a completed step."""
        ...
