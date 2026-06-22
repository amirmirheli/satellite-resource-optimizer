"""Seedable RNG helper for reproducible runs.

A thin wrapper over :class:`random.Random` so the rest of the code never reaches for the
global RNG (which would break determinism). ``derive`` produces independent, reproducible
sub-streams (e.g. one per component) from a single master seed.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import TypeVar

_T = TypeVar("_T")


class Rng:
    """Deterministic random source seeded from a single integer."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._random = random.Random(seed)

    @property
    def seed(self) -> int:
        return self._seed

    def derive(self, label: str) -> Rng:
        """Create an independent sub-stream keyed by ``label`` (stable across runs)."""
        return Rng(hash((self._seed, label)) & 0xFFFF_FFFF)

    def random(self) -> float:
        """Uniform float in ``[0.0, 1.0)``."""
        return self._random.random()

    def randint(self, low: int, high: int) -> int:
        """Uniform integer in the inclusive range ``[low, high]``."""
        return self._random.randint(low, high)

    def poisson(self, rate: float) -> int:
        """Draw a Poisson(``rate``) count using Knuth's algorithm (no numpy dependency)."""
        if rate <= 0.0:
            return 0
        import math

        target = math.exp(-rate)
        count = 0
        product = self._random.random()
        while product > target:
            count += 1
            product *= self._random.random()
        return count

    def weighted_choice(self, items: Sequence[tuple[float, _T]]) -> _T:
        """Weighted choice from ``(weight, item)`` pairs; weights need not be normalized."""
        if not items:
            raise ValueError("weighted_choice requires at least one item")
        total = sum(w for w, _ in items)
        threshold = self._random.random() * total
        cumulative = 0.0
        for weight, item in items:
            cumulative += weight
            if threshold <= cumulative:
                return item
        return items[-1][1]
