"""Unit tests for pure domain logic (derived properties + planning helpers)."""

from __future__ import annotations

import math

from satsim.domain.enums import Band, FleetId, Region, TrafficClass
from satsim.domain.models import (
    Beam,
    CongestionState,
    ConstellationSnapshot,
    Satellite,
    ServiceRequest,
)
from satsim.domain.planning import AdmissionCurve, ResourcePlan


def _beam(units: float) -> Beam:
    return Beam(
        beam_id="b", fleet=FleetId.NEXT_GEN, satellite_id="s",
        region=Region.NA, band=Band.KU, capacity_units=units,
    )


def test_snapshot_capacity_and_beams() -> None:
    sat = Satellite(satellite_id="s", fleet=FleetId.NEXT_GEN, beams=(_beam(3.0), _beam(2.0)))
    snap = ConstellationSnapshot(step=1, satellites=(sat,))
    assert len(snap.beams) == 2
    assert snap.total_capacity() == 5.0


def test_request_waiting_and_expiry() -> None:
    req = ServiceRequest(
        request_id="r", traffic_class=TrafficClass.MESSAGING, region=Region.NA,
        size_bytes=128, link_quality=0.9, urgency=0.1, arrival_step=5, deadline_step=10,
    )
    assert req.waiting_steps(8) == 3
    assert req.waiting_steps(2) == 0  # never negative
    assert not req.is_expired(10)
    assert req.is_expired(11)


def test_congestion_utilization_and_collapse_risk() -> None:
    c = CongestionState(
        step=1, queue_depth=0, offered_load_units=150.0, available_capacity_units=100.0
    )
    assert math.isclose(c.utilization, 1.5)
    assert 0.0 <= c.collapse_risk <= 1.0
    empty = CongestionState(
        step=1, queue_depth=0, offered_load_units=1.0, available_capacity_units=0.0
    )
    assert empty.utilization == float("inf")


def test_admission_curve_interpolation_monotone() -> None:
    curve = AdmissionCurve(breakpoints=((0.0, 0.0), (0.5, 0.5), (1.0, 1.0)))
    assert curve.admit_probability(-1.0) == 0.0  # clamped to first
    assert curve.admit_probability(2.0) == 1.0  # clamped to last
    assert math.isclose(curve.admit_probability(0.25), 0.25)
    assert math.isclose(curve.admit_probability(0.75), 0.75)


def test_admission_curve_constant() -> None:
    curve = AdmissionCurve.constant(0.3)
    assert math.isclose(curve.admit_probability(0.0), 0.3)
    assert math.isclose(curve.admit_probability(1.0), 0.3)


def test_resource_plan_passthrough_admits_everything() -> None:
    plan = ResourcePlan.passthrough(step=3)
    assert plan.generated_at_step == 3
    assert math.isclose(plan.admission_curve.admit_probability(0.0), 1.0)
