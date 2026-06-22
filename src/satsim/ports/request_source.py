"""Port: request ingestion (Kafka-style consume side).

Production ingestion was async/event-based (Kafka). Here the transport is abstracted:
the loop *polls* this port once per step, exactly like a consumer ``poll(timeout)``
draining available records. The default fake (``SyntheticRequestSource``) is backed by
the ``DemandGenerator`` publishing onto an in-memory bus; a real ``KafkaRequestSource``
is a documented future adapter that satisfies this same Protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from satsim.domain.models import ServiceRequest


@runtime_checkable
class RequestSource(Protocol):
    """Consume-side source of incoming service requests."""

    def poll(self, step: int, max_batch: int) -> list[ServiceRequest]:
        """Drain up to ``max_batch`` requests available at ``step``.

        Returns fewer than ``max_batch`` (possibly zero) when less is available.
        Anything not drained is backlog — the ingestion analogue of consumer lag —
        and remains available on subsequent polls.
        """
        ...
