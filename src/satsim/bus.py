"""In-memory request bus — the license-free stand-in for a Kafka topic.

Models a single-partition topic: producers ``publish`` requests tagged with the step they
become available, and a consumer ``drain``s up to a batch of *available* requests in FIFO
publish order. Anything not drained stays as backlog (the consumer-lag analogue), so the
loop naturally sees congestion build when it can't keep up.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from satsim.domain.models import ServiceRequest


@dataclass(slots=True)
class _Entry:
    available_at_step: int
    seq: int  # publish order, for stable FIFO draining
    request: ServiceRequest


class InMemoryBus:
    """FIFO request bus with step-gated availability (fake Kafka topic)."""

    def __init__(self) -> None:
        self._pending: deque[_Entry] = deque()
        self._seq: int = 0

    def publish(self, request: ServiceRequest, available_at_step: int) -> None:
        """Enqueue ``request``, visible to ``drain`` once ``step >= available_at_step``."""
        self._pending.append(_Entry(available_at_step, self._seq, request))
        self._seq += 1

    def drain(self, step: int, max_batch: int) -> list[ServiceRequest]:
        """Remove and return up to ``max_batch`` requests available at ``step``.

        Available entries are returned in publish order; entries not yet available (and
        any beyond ``max_batch``) remain queued. ``max_batch <= 0`` drains nothing.
        """
        if max_batch <= 0:
            return []
        drained: list[ServiceRequest] = []
        remaining: deque[_Entry] = deque()
        while self._pending:
            entry = self._pending.popleft()
            if len(drained) < max_batch and entry.available_at_step <= step:
                drained.append(entry.request)
            else:
                remaining.append(entry)  # not yet available, or batch already full
        self._pending = remaining
        return drained

    def __len__(self) -> int:
        """Current backlog size (undrained requests) — the consumer-lag analogue."""
        return len(self._pending)
