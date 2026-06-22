"""Tests for the Tier-2 emergency lane (EmergencyLane)."""

from __future__ import annotations

from satsim.adapters.emergency import EmergencyLane
from satsim.config import EmergencyConfig
from satsim.domain.enums import Band, FleetId, Region, RejectReason, TrafficClass
from satsim.domain.models import (
    Beam,
    ConstellationSnapshot,
    RegulatoryDecision,
    Satellite,
    ServiceRequest,
)
from satsim.domain.planning import ResourcePlan
from satsim.ports.admission import EmergencyAdmission

_FLEET = FleetId.NEXT_GEN
_BAND = Band.KU


class _AllowAll:
    """Regulatory stub that permits everything (lane only calls evaluate)."""

    def allowed_options(self, request: ServiceRequest) -> list:  # type: ignore[type-arg]
        return []

    def evaluate(self, region: Region, fleet: FleetId, band: Band) -> RegulatoryDecision:
        return RegulatoryDecision(allowed=True)


def _snapshot(*beams: tuple[Region, float]) -> ConstellationSnapshot:
    made = tuple(
        Beam(f"b{i}", _FLEET, "s", region, _BAND, cap)
        for i, (region, cap) in enumerate(beams)
    )
    return ConstellationSnapshot(step=0, satellites=(Satellite("s", _FLEET, made),))


def _sos(
    rid: str,
    *,
    region: Region = Region.NA,
    size: int = 256,
    link: float = 1.0,
    urgency: float = 0.9,
    arrival: int = 0,
    deadline: int = 20,
    retry: int = 0,
) -> ServiceRequest:
    return ServiceRequest(
        request_id=rid, traffic_class=TrafficClass.EMERGENCY_SOS, region=region, size_bytes=size,
        link_quality=link, urgency=urgency, arrival_step=arrival, deadline_step=deadline,
        retry_count=retry,
    )


def _lane(**overrides: float | int) -> EmergencyLane:
    cfg = EmergencyConfig(**overrides)  # type: ignore[arg-type]
    return EmergencyLane(cfg, _AllowAll())


def _plan() -> ResourcePlan:
    return ResourcePlan.passthrough()


def test_satisfies_port() -> None:
    assert isinstance(_lane(), EmergencyAdmission)


def test_reserves_only_the_reserved_fraction() -> None:
    # Beam capacity 100, reserved fraction 0.2 -> 20 units reserved for urgent.
    lane = _lane(reserved_fraction=0.2, max_units_per_request=100.0, geo_fairness_weight=1.0)
    snap = _snapshot((Region.NA, 100.0))
    # Each request costs 4096*?? -> use size to make cost 10 units: size = 10*4096, link 1.
    reqs = [_sos(f"r{i}", size=10 * 4096) for i in range(5)]  # 10 units each
    decision = lane.reserve(reqs, snap, _plan(), step=0)
    assert decision.reserved_units_used <= 20.0 + 1e-9  # never exceeds reserved pool
    assert len(decision.admitted) == 2  # only two 10-unit requests fit in 20 reserved
    assert all(r.reason is RejectReason.EMERGENCY_LANE_FULL for r in decision.shed)


def test_higher_urgency_served_first() -> None:
    lane = _lane(reserved_fraction=0.1, max_units_per_request=100.0)
    snap = _snapshot((Region.NA, 100.0))  # 10 units reserved
    low = _sos("low", size=10 * 4096, urgency=0.2)
    high = _sos("high", size=10 * 4096, urgency=0.95)
    decision = lane.reserve([low, high], snap, _plan(), step=0)
    assert [r.request_id for r in decision.admitted] == ["high"]


def test_per_request_cap_sheds_oversized_request() -> None:
    # A request that exceeds the emergency lane's per-request cap is not counted served.
    lane = _lane(reserved_fraction=1.0, max_units_per_request=4.0)
    snap = _snapshot((Region.NA, 100.0))
    poor = _sos("poor", size=256, link=0.05)  # raw cost = (256/4096)/0.05 = 1.25 -> under cap
    big = _sos("big", size=100 * 4096, link=1.0)  # raw cost 100 -> over cap
    decision = lane.reserve([big, poor], snap, _plan(), step=0)
    assert [a.request_id for a in decision.reservations] == ["poor"]
    assert any(r.request.request_id == "big" for r in decision.shed)


def test_geographic_fairness_caps_one_region() -> None:
    # Two regions, 50 reserved each (fraction 0.5 of 100). Equal share = 50 of 100 total.
    lane = _lane(reserved_fraction=0.5, max_units_per_request=100.0, geo_fairness_weight=1.0)
    snap = _snapshot((Region.NA, 100.0), (Region.EU, 100.0))  # 50 + 50 reserved = 100 total
    # Surge of 10-unit requests all in NA; NA may take at most its equal share (50).
    na_reqs = [_sos(f"na{i}", region=Region.NA, size=10 * 4096) for i in range(10)]
    decision = lane.reserve(na_reqs, snap, _plan(), step=0)
    assert decision.reserved_units_used <= 50.0 + 1e-9  # capped at NA's fair share


def test_expired_and_over_budget_are_shed_with_reasons() -> None:
    lane = _lane(reserved_fraction=1.0, max_retries=3)
    snap = _snapshot((Region.NA, 100.0))
    expired = _sos("exp", arrival=0, deadline=0)
    over = _sos("over", retry=4)
    decision = lane.reserve([expired, over], snap, _plan(), step=1)
    reasons = {r.request.request_id: r.reason for r in decision.shed}
    assert reasons["exp"] is RejectReason.DEADLINE_MISSED
    assert reasons["over"] is RejectReason.RETRY_BUDGET_EXHAUSTED


def test_no_eligible_beam_in_region_is_shed() -> None:
    lane = _lane(reserved_fraction=1.0)
    snap = _snapshot((Region.EU, 100.0))  # only EU beams
    decision = lane.reserve([_sos("na", region=Region.NA)], snap, _plan(), step=0)
    assert decision.admitted == ()
    assert decision.shed[0].reason is RejectReason.EMERGENCY_LANE_FULL


def test_reservations_target_real_beams() -> None:
    lane = _lane(reserved_fraction=0.5)
    snap = _snapshot((Region.NA, 40.0))
    decision = lane.reserve([_sos("r")], snap, _plan(), step=0)
    assert len(decision.reservations) == 1
    assert decision.reservations[0].beam_id == "b0"
    assert decision.reservations[0].fleet is _FLEET
