"""Tests for the RequestScore value type and the RequestScorer formula."""

from __future__ import annotations

from satsim.config import SimulationConfig
from satsim.domain.enums import Region, TrafficClass
from satsim.domain.models import RequestScore, ServiceRequest
from satsim.domain.planning import AdmissionCurve, ResourcePlan
from satsim.scoring import RequestScorer


def _req(
    tc: TrafficClass = TrafficClass.MESSAGING,
    *,
    size: int = 4096,
    link: float = 1.0,
    urgency: float = 0.0,
    arrival: int = 0,
    deadline: int = 10,
) -> ServiceRequest:
    return ServiceRequest(
        request_id="r", traffic_class=tc, region=Region.NA, size_bytes=size,
        link_quality=link, urgency=urgency, arrival_step=arrival, deadline_step=deadline,
    )


def test_request_score_holds_fields() -> None:
    s = RequestScore(score=0.5, estimated_cost_units=2.0, delivery_probability=0.9)
    assert (s.score, s.estimated_cost_units, s.delivery_probability) == (0.5, 2.0, 0.9)


def test_higher_priority_class_scores_higher() -> None:
    scorer = RequestScorer(SimulationConfig())
    plan = ResourcePlan.passthrough()
    sos = scorer.evaluate(_req(TrafficClass.EMERGENCY_SOS), 0, plan).score
    api = scorer.evaluate(_req(TrafficClass.THIRD_PARTY_API), 0, plan).score
    assert sos > api


def test_waiting_increases_score() -> None:
    scorer = RequestScorer(SimulationConfig())
    plan = ResourcePlan.passthrough()
    early = scorer.evaluate(_req(arrival=0, deadline=10), 0, plan).score
    late = scorer.evaluate(_req(arrival=0, deadline=10), 9, plan).score
    assert late > early


def test_cost_scales_with_size_and_inverse_link() -> None:
    scorer = RequestScorer(SimulationConfig())
    plan = ResourcePlan.passthrough()
    small = scorer.evaluate(_req(size=4096, link=1.0), 0, plan).estimated_cost_units
    big = scorer.evaluate(_req(size=40960, link=1.0), 0, plan).estimated_cost_units
    poor = scorer.evaluate(_req(size=4096, link=0.5), 0, plan).estimated_cost_units
    assert big > small
    assert poor > small  # worse link costs more airtime
    assert small == 1.0  # 4096 bytes / 4096 bytes-per-unit / link 1.0


def test_delivery_probability_tracks_link_quality() -> None:
    scorer = RequestScorer(SimulationConfig())
    s = scorer.evaluate(_req(link=0.7), 0, ResourcePlan.passthrough())
    assert s.delivery_probability == 0.7


def test_fairness_weight_scales_score() -> None:
    scorer = RequestScorer(SimulationConfig())
    base = scorer.evaluate(_req(TrafficClass.MESSAGING), 0, ResourcePlan.passthrough()).score
    boosted_plan = ResourcePlan(
        generated_at_step=0,
        valid_for_steps=1,
        admission_curve=AdmissionCurve.constant(1.0),
        fairness_weights={TrafficClass.MESSAGING: 0.5},
    )
    boosted = scorer.evaluate(_req(TrafficClass.MESSAGING), 0, boosted_plan).score
    assert boosted < base  # weight 0.5 halves the score


def test_scorer_is_deterministic() -> None:
    scorer = RequestScorer(SimulationConfig())
    plan = ResourcePlan.passthrough()
    a = scorer.evaluate(_req(urgency=0.3), 2, plan)
    b = scorer.evaluate(_req(urgency=0.3), 2, plan)
    assert a == b
