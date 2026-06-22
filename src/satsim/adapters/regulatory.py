"""Default :class:`~satsim.ports.regulatory.RegulatoryPolicy`: a per-region rule table.

Backed by the ``region_rules`` in :class:`~satsim.config.SimulationConfig`. A (fleet, band)
option is legal in a region iff the region's rule lists both the fleet and the band; the
rule's airtime/power caps ride along as :class:`RegulatoryConstraints` on each option.
"""

from __future__ import annotations

from satsim.config import RegionRule, SimulationConfig
from satsim.domain.enums import Band, FleetId, Region
from satsim.domain.models import (
    CandidateOption,
    RegulatoryConstraints,
    RegulatoryDecision,
    ServiceRequest,
)


class TableRegulatoryPolicy:
    """Looks up spectrum legality + caps from a per-region rule table."""

    def __init__(self, config: SimulationConfig) -> None:
        self._rules: dict[Region, RegionRule] = {rule.region: rule for rule in config.region_rules}

    def allowed_options(self, request: ServiceRequest) -> list[CandidateOption]:
        rule = self._rules.get(request.region)
        if rule is None:
            return []
        constraints = self._constraints(rule)
        options = [
            CandidateOption(fleet=fleet, band=band, constraints=constraints)
            for fleet in rule.allowed_fleets
            for band in rule.allowed_bands
        ]
        # Surface the request's preferred band first, if it is legal, so the scheduler can
        # honor it cheaply without re-checking legality.
        if request.preferred_band is not None:
            options.sort(key=lambda opt: opt.band != request.preferred_band)
        return options

    def evaluate(self, region: Region, fleet: FleetId, band: Band) -> RegulatoryDecision:
        rule = self._rules.get(region)
        if rule is None:
            return RegulatoryDecision(
                allowed=False, reason=f"no spectrum rule for region {region.value}"
            )
        if fleet not in rule.allowed_fleets:
            return RegulatoryDecision(
                allowed=False, reason=f"fleet {fleet.value} not licensed in {region.value}"
            )
        if band not in rule.allowed_bands:
            return RegulatoryDecision(
                allowed=False, reason=f"band {band.value} not permitted in {region.value}"
            )
        return RegulatoryDecision(allowed=True, constraints=self._constraints(rule))

    @staticmethod
    def _constraints(rule: RegionRule) -> RegulatoryConstraints:
        return RegulatoryConstraints(
            max_airtime_units=rule.max_airtime_units, max_power=rule.max_power
        )
