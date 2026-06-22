"""Resilience primitives for the control loop.

Currently the circuit breaker used to fail a flaky constellation over to the other fleet.
Kept deliberately small and side-effect free (it only tracks counts + a reopen step), so the
loop owns when to call it and what to do on each outcome.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CircuitBreaker:
    """Three-state breaker: CLOSED -> (failures) -> OPEN -> (cooldown) -> HALF-OPEN trial.

    Usage per step: call :meth:`allow`; if it returns ``True`` attempt the operation, then call
    :meth:`record_success` or :meth:`record_failure`. While OPEN (within cooldown) ``allow`` is
    ``False`` and the caller should fail over elsewhere without attempting.
    """

    failure_threshold: int
    cooldown_steps: int
    _failures: int = 0
    _open_until: int | None = None  # step at which a half-open trial is permitted; None = closed

    @property
    def is_open(self) -> bool:
        return self._open_until is not None

    def allow(self, step: int) -> bool:
        """Whether to attempt the protected call at ``step``."""
        if self._open_until is None:
            return True  # closed
        return step >= self._open_until  # half-open trial once cooldown has elapsed

    def record_success(self) -> None:
        """Reset to fully closed after a successful (possibly half-open trial) call."""
        self._failures = 0
        self._open_until = None

    def record_failure(self, step: int) -> bool:
        """Record a failure; return ``True`` iff this failure newly tripped the breaker open."""
        self._failures += 1
        if self._open_until is not None:
            # A half-open trial failed: extend the cooldown, but it was already open.
            self._open_until = step + self.cooldown_steps
            return False
        if self._failures >= self.failure_threshold:
            self._open_until = step + self.cooldown_steps
            return True
        return False
