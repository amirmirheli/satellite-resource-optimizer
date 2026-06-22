"""Default :class:`~satsim.ports.telemetry.TelemetrySink` adapters.

* :class:`ConsoleTelemetrySink` prints structured lines for interactive runs.
* :class:`InMemoryTelemetrySink` retains everything for assertions in tests, with a few
  convenience accessors so scenario tests can read back what happened.
"""

from __future__ import annotations

from satsim.domain.telemetry import StepCounters, TelemetryEvent


class InMemoryTelemetrySink:
    """Records events and per-step counters in memory for inspection/assertions."""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []
        self.steps: list[StepCounters] = []

    def emit(self, event: TelemetryEvent) -> None:
        self.events.append(event)

    def record_step(self, counters: StepCounters) -> None:
        self.steps.append(counters)

    def events_of_kind(self, kind: str) -> list[TelemetryEvent]:
        """All recorded events with the given ``kind``."""
        return [e for e in self.events if e.kind == kind]

    @property
    def last_step(self) -> StepCounters | None:
        """The most recently recorded step counters, if any."""
        return self.steps[-1] if self.steps else None

    def total_served(self) -> int:
        """Sum of ``served`` across all recorded steps."""
        return sum(s.served for s in self.steps)


class ConsoleTelemetrySink:
    """Prints structured telemetry to stdout (or any text writer)."""

    def __init__(self, *, emit_events: bool = False) -> None:
        # Per-event output is verbose; default to step summaries only.
        self._emit_events = emit_events

    def emit(self, event: TelemetryEvent) -> None:
        if not self._emit_events:
            return
        parts = [f"step={event.step}", f"kind={event.kind}"]
        if event.request_id is not None:
            parts.append(f"req={event.request_id}")
        if event.traffic_class is not None:
            parts.append(f"class={event.traffic_class.name}")
        if event.region is not None:
            parts.append(f"region={event.region.value}")
        if event.fleet is not None:
            parts.append(f"fleet={event.fleet.value}")
        if event.reason is not None:
            parts.append(f"reason={event.reason.value}")
        for key, value in event.detail.items():
            parts.append(f"{key}={value}")
        print("  ".join(parts))

    def record_step(self, counters: StepCounters) -> None:
        print(
            f"[step {counters.step:>4}] "
            f"admitted={counters.admitted} served={counters.served} "
            f"deferred={counters.deferred} dropped={counters.dropped} "
            f"rejected={counters.rejected} "
            f"queue={counters.queue_depth} "
            f"util={counters.utilization:.2f} fairness={counters.fairness_index:.2f} "
            f"collapse_risk={counters.collapse_risk:.2f} mode={counters.degrade_mode.value}"
        )
