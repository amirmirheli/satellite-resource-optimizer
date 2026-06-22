"""Port: regulatory / spectrum policy.

Different regions have different spectrum rules: allowed bands, per-region power/airtime
caps, and some constellations not licensed in some regions. An allocation that is fine in
one region may be illegal in another, so candidate (fleet, band) options are filtered per
request *before* scheduling.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from satsim.domain.enums import Band, FleetId, Region
from satsim.domain.models import CandidateOption, RegulatoryDecision, ServiceRequest


@runtime_checkable
class RegulatoryPolicy(Protocol):
    """Decides which (fleet, band) options are legal for a request's region."""

    def allowed_options(self, request: ServiceRequest) -> list[CandidateOption]:
        """Return every legal (fleet, band) option for ``request`` with its constraints.

        An empty list means no legal option exists in the request's region (the loop
        then rejects with ``RejectReason.NO_LEGAL_OPTION``).
        """
        ...

    def evaluate(self, region: Region, fleet: FleetId, band: Band) -> RegulatoryDecision:
        """Evaluate a single (region, fleet, band) triple: allow/deny + constraints."""
        ...
