"""Ports: the dependency boundary of the system.

Each port is a :class:`typing.Protocol`. The control logic depends only on these
interfaces; concrete adapters (license-free fakes by default, real systems later)
satisfy them structurally — no inheritance required, ``mypy --strict`` clean.

Ports
-----
* :class:`~satsim.ports.request_source.RequestSource` — Kafka-style consume side.
* :class:`~satsim.ports.constellation.ConstellationClient` — visible fleet resources.
* :class:`~satsim.ports.regulatory.RegulatoryPolicy` — per-region spectrum legality.
* :class:`~satsim.ports.admission.AdmissionController` — probabilistic load shaping.
* :class:`~satsim.ports.admission.EmergencyAdmission` — Tier-2 reactive lane.
* :class:`~satsim.ports.scheduler.ResourceScheduler` — core allocation.
* :class:`~satsim.ports.optimizer.Optimizer` — Tier-3 periodic global planner.
* :class:`~satsim.ports.telemetry.TelemetrySink` — structured observability.
"""
