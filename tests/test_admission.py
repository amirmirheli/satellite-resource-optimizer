"""Tests for the ProbabilisticAdmissionController."""

from __future__ import annotations

from satsim.adapters.admission import ProbabilisticAdmissionController
from satsim.domain.enums import Region, RejectReason, TrafficClass
from satsim.domain.models import (
    CongestionState,
    RequestScore,
    ScoredRequest,
    ServiceRequest,
)
from satsim.domain.planning import AdmissionCurve, ResourcePlan
from satsim.rng import Rng


def _scored(score: float, *, rid: str = "r") -> ScoredRequest:
    req = ServiceRequest(
        request_id=rid, traffic_class=TrafficClass.MESSAGING, region=Region.NA,
        size_bytes=128, link_quality=0.9, urgency=0.0, arrival_step=0, deadline_step=5,
    )
    return ScoredRequest(req, RequestScore(score=score, estimated_cost_units=1.0,
                                          delivery_probability=0.9))


def _calm(util: float = 0.5) -> CongestionState:
    return CongestionState(
        step=0, queue_depth=0, offered_load_units=util * 100.0, available_capacity_units=100.0
    )


def _plan(curve: AdmissionCurve) -> ResourcePlan:
    return ResourcePlan(generated_at_step=0, valid_for_steps=1, admission_curve=curve)


def test_passthrough_admits_all_when_calm() -> None:
    ac = ProbabilisticAdmissionController(Rng(0))  # default passthrough plan
    result = ac.admit([_scored(0.1), _scored(0.9)], _calm(), step=0)
    assert len(result.admitted) == 2
    assert result.rejected == ()


def test_zero_curve_rejects_all_with_reason() -> None:
    ac = ProbabilisticAdmissionController(Rng(0), _plan(AdmissionCurve.constant(0.0)))
    result = ac.admit([_scored(0.9)], _calm(), step=0)
    assert result.admitted == ()
    assert len(result.rejected) == 1
    assert result.rejected[0].reason is RejectReason.ADMISSION_SHED


def test_congestion_damps_low_scores_but_spares_high() -> None:
    # Overage = 1.0 (utilization 2.0): score=0 -> prob 0 (always shed);
    # score=1 -> prob 1 (always admitted). Deterministic regardless of seed.
    ac = ProbabilisticAdmissionController(Rng(0), _plan(AdmissionCurve.constant(1.0)))
    congested = CongestionState(
        step=0, queue_depth=0, offered_load_units=200.0, available_capacity_units=100.0
    )
    result = ac.admit([_scored(0.0, rid="low"), _scored(1.0, rid="high")], congested, step=0)
    admitted_ids = {s.request.request_id for s in result.admitted}
    assert admitted_ids == {"high"}


def test_queue_pressure_damps_low_scores_even_when_polled_load_is_low() -> None:
    ac = ProbabilisticAdmissionController(Rng(0), _plan(AdmissionCurve.constant(1.0)))
    lagging = CongestionState(
        step=0, queue_depth=100, offered_load_units=50.0, available_capacity_units=100.0
    )
    result = ac.admit([_scored(0.0, rid="low"), _scored(1.0, rid="high")], lagging, step=0)
    admitted_ids = {s.request.request_id for s in result.admitted}
    assert admitted_ids == {"high"}


def test_update_policy_changes_behavior() -> None:
    ac = ProbabilisticAdmissionController(Rng(0), _plan(AdmissionCurve.constant(0.0)))
    assert ac.admit([_scored(0.9)], _calm(), step=0).admitted == ()
    ac.update_policy(_plan(AdmissionCurve.constant(1.0)))
    assert len(ac.admit([_scored(0.9)], _calm(), step=0).admitted) == 1


def test_admitted_requests_keep_their_scores() -> None:
    ac = ProbabilisticAdmissionController(Rng(0))
    result = ac.admit([_scored(0.7)], _calm(), step=0)
    assert result.admitted[0].scoring.score == 0.7
