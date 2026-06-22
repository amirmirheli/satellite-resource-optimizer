"""Port-conformance smoke tests.

Confirms every port Protocol is importable and that a trivial stub satisfies it
structurally (``runtime_checkable`` isinstance). This catches signature/shape drift
between ports and the domain types they exchange before any real implementation exists.
"""

from __future__ import annotations

from collections.abc import Sequence

from satsim.domain.enums import Band, FleetId, Region
from satsim.domain.models import (
    AdmissionResult,
    CandidateOption,
    CongestionState,
    ConstellationSnapshot,
    RegulatoryDecision,
    ScheduleCandidate,
    SchedulingResult,
    ScoredRequest,
    ServiceRequest,
)
from satsim.domain.planning import (
    EmergencyDecision,
    PlanningWindow,
    ResourcePlan,
)
from satsim.domain.telemetry import StepCounters, TelemetryEvent
from satsim.ports.admission import AdmissionController, EmergencyAdmission
from satsim.ports.constellation import ConstellationClient
from satsim.ports.optimizer import Optimizer
from satsim.ports.regulatory import RegulatoryPolicy
from satsim.ports.request_source import RequestSource
from satsim.ports.scheduler import ResourceScheduler
from satsim.ports.telemetry import TelemetrySink


class _StubRequestSource:
    def poll(self, step: int, max_batch: int) -> list[ServiceRequest]:
        return []


class _StubConstellation:
    def fleet_id(self) -> FleetId:
        return FleetId.LEGACY_LEO

    def visible_resources(self, step: int) -> ConstellationSnapshot:
        return ConstellationSnapshot(step=step)


class _StubRegulatory:
    def allowed_options(self, request: ServiceRequest) -> list[CandidateOption]:
        return []

    def evaluate(self, region: Region, fleet: FleetId, band: Band) -> RegulatoryDecision:
        return RegulatoryDecision(allowed=True)


class _StubAdmission:
    def admit(
        self, scored: Sequence[ScoredRequest], congestion: CongestionState, step: int
    ) -> AdmissionResult:
        return AdmissionResult(admitted=tuple(scored))

    def update_policy(self, plan: ResourcePlan) -> None:
        return None


class _StubEmergency:
    def reserve(
        self,
        urgent: Sequence[ServiceRequest],
        snapshot: ConstellationSnapshot,
        plan: ResourcePlan,
        step: int,
    ) -> EmergencyDecision:
        return EmergencyDecision()


class _StubScheduler:
    def schedule(
        self,
        candidates: Sequence[ScheduleCandidate],
        snapshot: ConstellationSnapshot,
        step: int,
    ) -> SchedulingResult:
        return SchedulingResult()


class _StubOptimizer:
    def plan(self, window: PlanningWindow) -> ResourcePlan:
        return ResourcePlan.passthrough(step=window.step)


class _StubTelemetry:
    def emit(self, event: TelemetryEvent) -> None:
        return None

    def record_step(self, counters: StepCounters) -> None:
        return None


def test_stubs_satisfy_ports() -> None:
    assert isinstance(_StubRequestSource(), RequestSource)
    assert isinstance(_StubConstellation(), ConstellationClient)
    assert isinstance(_StubRegulatory(), RegulatoryPolicy)
    assert isinstance(_StubAdmission(), AdmissionController)
    assert isinstance(_StubEmergency(), EmergencyAdmission)
    assert isinstance(_StubScheduler(), ResourceScheduler)
    assert isinstance(_StubOptimizer(), Optimizer)
    assert isinstance(_StubTelemetry(), TelemetrySink)


def test_incomplete_stub_does_not_satisfy_port() -> None:
    class _Missing:
        def emit(self, event: TelemetryEvent) -> None:
            return None

    assert not isinstance(_Missing(), TelemetrySink)
