"""I/O adapters: Kafka-style request ingestion and structured telemetry sinks."""

from satsim.adapters.io.request_source import SyntheticRequestSource
from satsim.adapters.io.telemetry import ConsoleTelemetrySink, InMemoryTelemetrySink

__all__ = ["ConsoleTelemetrySink", "InMemoryTelemetrySink", "SyntheticRequestSource"]
