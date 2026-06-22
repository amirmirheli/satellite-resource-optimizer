"""Run-level settings loaded from the environment / ``.env`` (pydantic-settings).

These are the *run* knobs — seed, duration, scheduler, optimizer backend, solver time limit,
verbosity — that a user commonly tweaks per invocation without editing code. Deep algorithm
tunables live in :mod:`satsim.config` sub-models with sensible defaults.

Precedence (applied by the CLI): explicit CLI flag > environment / ``.env`` > scenario default.
Env vars are prefixed ``SATSIM_`` (e.g. ``SATSIM_SEED=7``, ``SATSIM_SCHEDULER=heuristic``).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from satsim.domain.enums import OptimizerBackend, SchedulerKind


class RunSettings(BaseSettings):
    """Environment-driven run knobs; only env-set fields count as overrides (see overrides())."""

    model_config = SettingsConfigDict(
        env_prefix="SATSIM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    seed: int = 0
    steps: int | None = None
    scheduler: SchedulerKind = SchedulerKind.PRIORITY_FAIR
    optimizer_backend: OptimizerBackend = OptimizerBackend.HEURISTIC
    solver_time_limit_s: float = 0.5
    verbose: bool = False

    def overrides(self) -> set[str]:
        """Names of fields that were explicitly provided by the env / .env (not defaults)."""
        return set(self.model_fields_set)
