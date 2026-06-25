"""The external network environment: visible constellation fleets and per-region spectrum rules."""

from satsim.adapters.network.constellation import FakeConstellation, build_fake_constellations
from satsim.adapters.network.regulatory import TableRegulatoryPolicy

__all__ = ["FakeConstellation", "TableRegulatoryPolicy", "build_fake_constellations"]
