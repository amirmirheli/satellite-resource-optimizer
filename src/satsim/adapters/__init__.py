"""Adapters: concrete, license-free implementations of the ports.

These are the default, dependency-free fakes that let the entire system run, test, and
containerize with zero external services. Each satisfies a port in
:mod:`satsim.ports` structurally. A real adapter (Kafka, a live constellation client)
could be added here later without touching the core.
"""
